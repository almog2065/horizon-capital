"""Health and metrics endpoints."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from ...core import health as health_mod
from ...core.settings import get_settings
from ...metrics_registry import build_ops_summary, build_prometheus_text

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness — process is up. Used by container restart policy."""
    return JSONResponse(health_mod.liveness())


@router.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness — process can serve real traffic. Used by LB."""
    payload = health_mod.readiness()
    status_code = 200 if payload["status"] in ("ok", "degraded") else 503
    return JSONResponse(payload, status_code=status_code)


@router.get("/version")
def version() -> dict:
    cfg = get_settings()
    try:
        from ... import model_routing
        routing = model_routing.routing_table()
    except Exception:
        routing = {}
    return {
        "app": cfg.APP_NAME,
        "env": cfg.APP_ENV,
        "llm_mode": "mock" if cfg.derived_mock() else "live",
        "model_routing": routing,
    }


@router.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    """Prometheus exposition — counters, gauges, eval snapshot."""
    return build_prometheus_text()


@router.get("/api/observability/summary")
def observability_summary() -> dict:
    """JSON ops snapshot for UI and scripting."""
    return build_ops_summary()
