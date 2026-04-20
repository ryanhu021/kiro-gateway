# -*- coding: utf-8 -*-

"""Tests for kiro.metrics module."""

import asyncio
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from kiro.metrics import MetricsClient, MetricDatum, RequestMetricsContext


# =============================================================================
# MetricDatum
# =============================================================================

class TestMetricDatum:
    def test_creates_with_defaults(self):
        d = MetricDatum(name="Foo", value=1.0, unit="Count", dimensions={})
        assert d.name == "Foo"
        assert d.value == 1.0
        assert d.unit == "Count"
        assert d.timestamp > 0

    def test_frozen(self):
        d = MetricDatum(name="Foo", value=1.0, unit="Count", dimensions={})
        with pytest.raises(AttributeError):
            d.name = "Bar"


# =============================================================================
# RequestMetricsContext
# =============================================================================

class TestRequestMetricsContext:
    def test_defaults(self):
        ctx = RequestMetricsContext()
        assert ctx.kiro_request_start is None
        assert ctx.first_token_time is None
        assert ctx.kiro_request_end is None
        assert ctx.input_tokens == 0
        assert ctx.output_tokens == 0
        assert ctx.retry_count == 0

    def test_mutable(self):
        ctx = RequestMetricsContext()
        ctx.kiro_request_start = 100.0
        ctx.first_token_time = 101.5
        ctx.input_tokens = 500
        ctx.output_tokens = 200
        assert ctx.kiro_request_start == 100.0
        assert ctx.first_token_time == 101.5


# =============================================================================
# MetricsClient — disabled mode (default)
# =============================================================================

class TestMetricsClientDisabled:
    def test_put_is_noop_when_disabled(self):
        client = MetricsClient()
        assert client._enabled is False
        client.put("RequestCount", 1, "Count", {"api_format": "openai"})
        assert len(client._buffer) == 0

    def test_record_duration_is_noop_when_disabled(self):
        client = MetricsClient()
        client.record_duration("Duration", time.time() - 1.0)
        assert len(client._buffer) == 0

    def test_record_count_is_noop_when_disabled(self):
        client = MetricsClient()
        client.record_count("ErrorCount")
        assert len(client._buffer) == 0

    @pytest.mark.asyncio
    async def test_start_logs_disabled(self):
        client = MetricsClient()
        await client.start()
        assert client._cw_client is None
        assert client._flush_task is None

    @pytest.mark.asyncio
    async def test_stop_is_safe_when_disabled(self):
        client = MetricsClient()
        await client.stop()  # should not raise


# =============================================================================
# MetricsClient — enabled mode
# =============================================================================

class TestMetricsClientEnabled:
    def _make_enabled_client(self):
        client = MetricsClient()
        client._enabled = True
        return client

    def test_put_buffers_datum(self):
        client = self._make_enabled_client()
        client.put("RequestCount", 1, "Count", {"api_format": "openai"})
        assert len(client._buffer) == 1
        datum = client._buffer[0]
        assert datum.name == "RequestCount"
        assert datum.value == 1
        assert datum.unit == "Count"
        assert datum.dimensions == {"api_format": "openai"}

    def test_record_duration_computes_ms(self):
        client = self._make_enabled_client()
        start = time.time() - 2.0  # 2 seconds ago
        client.record_duration("Duration", start)
        datum = client._buffer[0]
        assert datum.name == "Duration"
        assert datum.unit == "Milliseconds"
        assert datum.value >= 1900  # at least ~2000ms

    def test_record_count_defaults_to_one(self):
        client = self._make_enabled_client()
        client.record_count("ErrorCount", dimensions={"error_type": "timeout"})
        datum = client._buffer[0]
        assert datum.value == 1
        assert datum.dimensions == {"error_type": "timeout"}

    def test_put_without_dimensions(self):
        client = self._make_enabled_client()
        client.put("RetryCount", 3, "Count")
        datum = client._buffer[0]
        assert datum.dimensions == {}

    def test_to_cw_format(self):
        datum = MetricDatum(
            name="Duration",
            value=150.5,
            unit="Milliseconds",
            dimensions={"api_format": "openai", "model": "claude-sonnet-4"},
        )
        cw = MetricsClient._to_cw(datum)
        assert cw["MetricName"] == "Duration"
        assert cw["Value"] == 150.5
        assert cw["Unit"] == "Milliseconds"
        assert len(cw["Dimensions"]) == 2
        dim_names = {d["Name"] for d in cw["Dimensions"]}
        assert dim_names == {"api_format", "model"}

    def test_to_cw_no_dimensions(self):
        datum = MetricDatum(name="Foo", value=1, unit="Count", dimensions={})
        cw = MetricsClient._to_cw(datum)
        assert "Dimensions" not in cw

    @pytest.mark.asyncio
    async def test_flush_calls_put_metric_data(self):
        client = self._make_enabled_client()
        mock_cw = MagicMock()
        mock_cw.put_metric_data = MagicMock()
        client._cw_client = mock_cw

        client.put("RequestCount", 1, "Count", {"api_format": "openai"})
        client.put("ErrorCount", 1, "Count", {"error_type": "timeout"})

        await client._flush()

        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args
        assert call_kwargs[1]["Namespace"] == "KiroGateway"
        assert len(call_kwargs[1]["MetricData"]) == 2

    @pytest.mark.asyncio
    async def test_flush_drains_buffer(self):
        client = self._make_enabled_client()
        mock_cw = MagicMock()
        mock_cw.put_metric_data = MagicMock()
        client._cw_client = mock_cw

        for i in range(5):
            client.put("RequestCount", 1, "Count")

        await client._flush()
        assert len(client._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_handles_boto3_error(self):
        client = self._make_enabled_client()
        mock_cw = MagicMock()
        mock_cw.put_metric_data = MagicMock(side_effect=Exception("throttled"))
        client._cw_client = mock_cw

        client.put("RequestCount", 1, "Count")
        # Should not raise
        await client._flush()
        assert len(client._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_noop_when_buffer_empty(self):
        client = self._make_enabled_client()
        mock_cw = MagicMock()
        client._cw_client = mock_cw

        await client._flush()
        mock_cw.put_metric_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_batches_at_25(self):
        client = self._make_enabled_client()
        mock_cw = MagicMock()
        mock_cw.put_metric_data = MagicMock()
        client._cw_client = mock_cw

        for i in range(30):
            client.put("RequestCount", 1, "Count")

        await client._flush()
        assert mock_cw.put_metric_data.call_count == 2
        first_call = mock_cw.put_metric_data.call_args_list[0]
        assert len(first_call[1]["MetricData"]) == 25
        second_call = mock_cw.put_metric_data.call_args_list[1]
        assert len(second_call[1]["MetricData"]) == 5

    @pytest.mark.asyncio
    async def test_stop_flushes_remaining(self):
        client = self._make_enabled_client()
        mock_cw = MagicMock()
        mock_cw.put_metric_data = MagicMock()
        client._cw_client = mock_cw

        client.put("RequestCount", 1, "Count")
        await client.stop()
        mock_cw.put_metric_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_with_missing_boto3(self):
        client = MetricsClient()
        client._enabled = True
        with patch.dict("sys.modules", {"boto3": None}):
            with patch("builtins.__import__", side_effect=ImportError("no boto3")):
                await client.start()
        assert client._enabled is False
