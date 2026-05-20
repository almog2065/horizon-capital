"""Structured logging configuration.

Single `setup_logging()` entry point that:
* Honors LOG_LEVEL and LOG_FORMAT from settings.
* Emits JSON in production (one event per line, parseable by Loki/CloudWatch).
* Emits human-readable text in dev.
* Quiets noisy third-party loggers.
* Installs a Uvicorn-compatible handler so access logs flow through the
  same formatter.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from .settings import get_settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # forward common structured fields
        for key in ("trace_id", "run_id", "ticker", "agent", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def setup_logging(force: bool = False) -> None:
    """Configure root + uvicorn loggers exactly once."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    cfg = get_settings()
    level = getattr(logging, cfg.LOG_LEVEL, logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if cfg.LOG_FORMAT == "json" else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # quiet down noisy libs
    for noisy in ("httpx", "httpcore", "yfinance", "asyncio", "urllib3", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # bridge uvicorn's three loggers to our handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [handler]
        lg.propagate = False
        lg.setLevel(level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Logger accessor that guarantees setup_logging() has been called."""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)
