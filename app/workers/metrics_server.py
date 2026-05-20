"""Lightweight /metrics on the worker — survives web outages.

Prometheus scrapes this job so `horizon_alerts_open` and cadence gauges stay
fresh when `horizon-web` is down. Same text format as the web tier.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core.logging import get_logger

log = get_logger("horizon.worker.metrics")

_server: ThreadingHTTPServer | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._ok(b'{"ok":true,"service":"worker-metrics"}\n', "application/json")
            return
        if path == "/metrics":
            from ..metrics_registry import build_prometheus_text
            self._ok(build_prometheus_text().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8")
            return
        self.send_error(404)

    def _ok(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_metrics_server(host: str = "0.0.0.0", port: int = 9091) -> None:
    global _server
    if _server is not None:
        return
    _server = ThreadingHTTPServer((host, port), _Handler)
    thread = threading.Thread(target=_server.serve_forever, name="worker-metrics", daemon=True)
    thread.start()
    log.info("worker-metrics-listening host=%s port=%s", host, port)
