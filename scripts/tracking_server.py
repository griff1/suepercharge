#!/usr/bin/env python
"""Local HTTP server wrapping the tracking Lambda handler.

Run directly:
    uv run scripts/tracking_server.py

Then expose via ngrok:
    ngrok http 8787

Set TRACKING_WEBHOOK_URL to the ngrok URL + /t  (e.g. https://abc123.ngrok.io/t)
so that emails' open pixels and CTA links route back here.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.tracking import handler as lambda_handler  # noqa: E402

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

PORT = int(os.environ.get("TRACKING_PORT", "8787"))


class TrackingHandler(BaseHTTPRequestHandler):
    """Translates HTTP GET → Lambda Function URL event → HTTP response."""

    def do_GET(self) -> None:
        event = {
            "rawPath": self.path,
            "headers": dict(self.headers),
        }
        resp = lambda_handler(event, None)

        status = resp.get("statusCode", 200)
        headers = resp.get("headers", {})
        is_base64 = resp.get("isBase64Encoded", False)

        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

        body = resp.get("body", "")
        if is_base64:
            import base64
            self.wfile.write(base64.b64decode(body))
        elif isinstance(body, str):
            self.wfile.write(body.encode())
        else:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        log.info(fmt, *args)


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), TrackingHandler)
    log.info("tracking server listening on http://0.0.0.0:%d", PORT)
    log.info("expose with: ngrok http %d", PORT)
    log.info("then set TRACKING_WEBHOOK_URL=https://<ngrok-id>.ngrok.io/t")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
