"""
mitmproxy addon to capture Kiro API traffic.

Setup:
    1. brew install mitmproxy
    2. Trust the mitmproxy CA cert (requires password prompt):
       security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db ~/.mitmproxy/mitmproxy-ca-cert.pem
    3. To remove trust when done:
       security remove-trusted-cert ~/.mitmproxy/mitmproxy-ca-cert.pem

Usage:
    Terminal 1: mitmdump -s scripts/capture_kiro.py --listen-port 8080 --ssl-insecure
    Terminal 2: HTTPS_PROXY=http://localhost:8080 kiro-cli chat

Captures are saved to scripts/captures/ as JSON files.
"""

import json
import os
from datetime import datetime
from mitmproxy import http

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "captures")
os.makedirs(OUTPUT_DIR, exist_ok=True)

counter = 0


def request(flow: http.HTTPFlow):
    global counter

    # Only capture requests to the Kiro API
    if "amazonaws.com" not in (flow.request.host or ""):
        return

    counter += 1
    timestamp = datetime.now().strftime("%H%M%S")
    prefix = f"{OUTPUT_DIR}/{counter:03d}_{timestamp}"

    # Save headers
    headers = dict(flow.request.headers)
    with open(f"{prefix}_req_headers.json", "w") as f:
        json.dump(headers, f, indent=2)

    # Save body
    body = flow.request.get_text()
    if body:
        try:
            parsed = json.loads(body)
            with open(f"{prefix}_req_body.json", "w") as f:
                json.dump(parsed, f, indent=2)
        except json.JSONDecodeError:
            with open(f"{prefix}_req_body.txt", "w") as f:
                f.write(body)

    url = flow.request.url
    method = flow.request.method
    target = headers.get("x-amz-target", "unknown")
    print(f"\n{'='*80}")
    print(f"[{counter}] {method} {url}")
    print(f"    x-amz-target: {target}")
    print(f"    Saved to: {prefix}_req_*")


def response(flow: http.HTTPFlow):
    if "amazonaws.com" not in (flow.request.host or ""):
        return

    timestamp = datetime.now().strftime("%H%M%S")
    # Find matching request number
    prefix = None
    for f in sorted(os.listdir(OUTPUT_DIR), reverse=True):
        if f.endswith("_req_headers.json"):
            prefix = os.path.join(OUTPUT_DIR, f.replace("_req_headers.json", ""))
            break

    if not prefix:
        return

    # Save response headers
    headers = dict(flow.response.headers)
    with open(f"{prefix}_res_headers.json", "w") as f:
        json.dump(headers, f, indent=2)

    # Save response body (may be streaming SSE)
    body = flow.response.get_text()
    if body:
        with open(f"{prefix}_res_body.txt", "w") as f:
            f.write(body)

    print(f"    Response: {flow.response.status_code} ({len(body or '')} bytes)")
    print(f"    Saved to: {prefix}_res_*")
