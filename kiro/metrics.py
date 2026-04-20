# -*- coding: utf-8 -*-

"""
CloudWatch metrics for Kiro Gateway.

Collects and publishes metrics to CloudWatch in batches.
Runs a background flush loop so request handlers never block on PutMetricData.

When CLOUDWATCH_METRICS_ENABLED is false (default for local dev), all calls
are no-ops — zero overhead.
"""

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence

from loguru import logger

NAMESPACE = os.environ.get("CW_METRICS_NAMESPACE", "KiroGateway")
FLUSH_INTERVAL = int(os.environ.get("CW_METRICS_FLUSH_INTERVAL", "60"))
MAX_BUFFER_SIZE = int(os.environ.get("CW_METRICS_BUFFER_SIZE", "500"))
ENABLED = os.environ.get("CLOUDWATCH_METRICS_ENABLED", "").lower() in ("1", "true", "yes")


@dataclass
class RequestMetricsContext:
    """
    Mutable context populated during a single request lifecycle.

    Created in the route handler, passed into streaming functions,
    and read back in the route handler's finally block to emit metrics.
    """
    kiro_request_start: Optional[float] = None   # set just before HTTP call to Kiro
    first_token_time: Optional[float] = None      # set when first content/thinking token arrives
    kiro_request_end: Optional[float] = None      # set when stream finishes
    input_tokens: int = 0
    output_tokens: int = 0
    retry_count: int = 0


@dataclass(frozen=True)
class MetricDatum:
    """Single metric data point ready for CloudWatch."""
    name: str
    value: float
    unit: str
    dimensions: Dict[str, str]
    timestamp: float = field(default_factory=time.time)


class MetricsClient:
    """
    Async-friendly CloudWatch metrics client with background flushing.

    Usage:
        metrics = MetricsClient()
        await metrics.start()          # call once at app startup
        metrics.put("RequestCount", 1, "Count", {"api_format": "openai"})
        await metrics.stop()           # call once at app shutdown
    """

    def __init__(self) -> None:
        self._buffer: Deque[MetricDatum] = deque(maxlen=MAX_BUFFER_SIZE * 2)
        self._flush_task: Optional[asyncio.Task] = None
        self._cw_client = None
        self._enabled = ENABLED

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise boto3 client and start background flush loop."""
        if not self._enabled:
            logger.info("CloudWatch metrics disabled (set CLOUDWATCH_METRICS_ENABLED=true to enable)")
            return

        try:
            import boto3
            self._cw_client = boto3.client("cloudwatch", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
            logger.info(f"CloudWatch metrics enabled (namespace={NAMESPACE}, flush_interval={FLUSH_INTERVAL}s)")
        except Exception as e:
            logger.warning(f"Failed to create CloudWatch client, metrics disabled: {e}")
            self._enabled = False
            return

        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Flush remaining metrics and cancel background task."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(
        self,
        name: str,
        value: float,
        unit: str,
        dimensions: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Record a metric data point (non-blocking).

        Args:
            name: Metric name (e.g. "RequestCount")
            value: Metric value
            unit: CloudWatch unit (Count, Milliseconds, None, etc.)
            dimensions: Key-value dimension pairs
        """
        if not self._enabled:
            return
        self._buffer.append(MetricDatum(
            name=name,
            value=value,
            unit=unit,
            dimensions=dimensions or {},
        ))

    def record_duration(
        self,
        name: str,
        start_time: float,
        dimensions: Optional[Dict[str, str]] = None,
    ) -> None:
        """Convenience: record elapsed time in milliseconds since start_time."""
        elapsed_ms = (time.time() - start_time) * 1000
        self.put(name, elapsed_ms, "Milliseconds", dimensions)

    def record_count(
        self,
        name: str,
        value: float = 1,
        dimensions: Optional[Dict[str, str]] = None,
    ) -> None:
        """Convenience: record a count metric."""
        self.put(name, value, "Count", dimensions)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Background loop that flushes buffered metrics periodically."""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await self._flush()

    async def _flush(self) -> None:
        """Send buffered metrics to CloudWatch in batches of 25."""
        if not self._cw_client or not self._buffer:
            return

        # Drain buffer
        items: List[MetricDatum] = []
        while self._buffer:
            items.append(self._buffer.popleft())

        # CloudWatch accepts max 1000 MetricData per call, but we batch at 25
        # to keep payloads small and reduce chance of partial failures.
        for i in range(0, len(items), 25):
            batch = items[i : i + 25]
            metric_data = [self._to_cw(d) for d in batch]
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda md=metric_data: self._cw_client.put_metric_data(
                        Namespace=NAMESPACE,
                        MetricData=md,
                    ),
                )
            except Exception as e:
                logger.warning(f"Failed to flush {len(batch)} metrics to CloudWatch: {e}")

    @staticmethod
    def _to_cw(datum: MetricDatum) -> dict:
        """Convert MetricDatum to CloudWatch PutMetricData format."""
        entry: dict = {
            "MetricName": datum.name,
            "Value": datum.value,
            "Unit": datum.unit,
        }
        if datum.dimensions:
            entry["Dimensions"] = [
                {"Name": k, "Value": v} for k, v in datum.dimensions.items()
            ]
        return entry


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
metrics = MetricsClient()
