"""Health check primitives.

Each probe returns a dict like::

    {"status": "ok"|"degraded"|"fail", "detail": "...", "latency_ms": 12}

Used by the /healthz and /readyz routes and by the docker healthcheck
script in docker/web/healthcheck.py.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .logging import get_logger
from .settings import get_settings

log = get_logger("horizon.health")


def _time_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def check_sqlite(path: Path, timeout: float = 2.0) -> dict[str, Any]:
    start = time.monotonic()
    try:
        if not path.exists():
            return {"status": "degraded", "detail": "sqlite-missing", "latency_ms": _time_ms(start), "path": str(path)}
        with sqlite3.connect(str(path), timeout=timeout) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "latency_ms": _time_ms(start), "path": str(path)}
    except Exception as e:
        return {"status": "fail", "detail": str(e), "latency_ms": _time_ms(start), "path": str(path)}


def check_redis() -> dict[str, Any]:
    cfg = get_settings()
    if not cfg.REDIS_URL:
        return {"status": "skipped", "detail": "no REDIS_URL"}
    start = time.monotonic()
    try:
        import redis  # type: ignore

        client = redis.from_url(cfg.REDIS_URL, socket_connect_timeout=2)
        client.ping()
        return {"status": "ok", "latency_ms": _time_ms(start)}
    except ImportError:
        return {"status": "skipped", "detail": "redis lib not installed"}
    except Exception as e:
        return {"status": "fail", "detail": str(e), "latency_ms": _time_ms(start)}


def liveness() -> dict[str, Any]:
    """Cheap probe: is the process running?"""
    return {"status": "ok"}


def readiness() -> dict[str, Any]:
    """Deep probe: are downstreams reachable?"""
    cfg = get_settings()
    checks = {
        "firm_db": check_sqlite(cfg.FIRM_DB, cfg.HEALTH_DB_TIMEOUT_SEC),
        "vector_db": check_sqlite(cfg.VECTOR_DB, cfg.HEALTH_DB_TIMEOUT_SEC),
        "redis": check_redis(),
    }
    overall = "ok"
    for c in checks.values():
        if c.get("status") == "fail":
            overall = "fail"
            break
        if c.get("status") == "degraded" and overall == "ok":
            overall = "degraded"
    return {"status": overall, "checks": checks}
