"""FastAPI app factory.

Composition root for the web tier. We:
1. Patch the legacy `app.main.app` lifespan with the new lifecycle.
2. Mount production-grade health/version/metrics routes.
3. Attach middleware (request id, CORS, trusted hosts).
4. Re-export the configured app for `uvicorn app.api.app_factory:app`.

Keeping the legacy main intact means the rich UI keeps working while
we add ops surface area in one place.
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from ..core.lifecycle import lifespan
from ..core.logging import get_logger, setup_logging
from ..core.settings import get_settings

log = get_logger("horizon.api")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Tag every request with an X-Request-Id (existing or fresh)."""

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request-failed", extra={"event": "http", "request_id": rid})
            raise
        response.headers["x-request-id"] = rid
        return response


def _attach_middleware(app: FastAPI) -> None:
    cfg = get_settings()

    app.add_middleware(RequestIdMiddleware)

    if cfg.CORS_ORIGINS:
        origins = [o.strip() for o in cfg.CORS_ORIGINS.split(",") if o.strip()]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    if cfg.ALLOWED_HOSTS and cfg.ALLOWED_HOSTS != "*":
        hosts = [h.strip() for h in cfg.ALLOWED_HOSTS.split(",") if h.strip()]
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)


def create_app() -> FastAPI:
    """Build the FastAPI app — single composition point."""
    setup_logging()
    cfg = get_settings()
    log.info("app-factory-start env=%s mock=%s", cfg.APP_ENV, cfg.derived_mock())

    # Import the legacy app *after* logging is configured. We reuse the
    # existing FastAPI instance so all the firm UI routes light up.
    from .. import main as legacy_main

    app: FastAPI = legacy_main.app
    # Replace the legacy lifespan with our production one.
    app.router.lifespan_context = lifespan
    app.title = f"Horizon Capital — {cfg.APP_ENV}"

    _attach_middleware(app)

    from ..core.exceptions import register_exception_handlers
    register_exception_handlers(app)

    from .. import ops_alerts

    def _ops_alert_summary() -> dict:
        return ops_alerts.summary()

    legacy_main.TEMPLATES.env.globals["ops_alert_summary"] = _ops_alert_summary

    # Mount our new ops + reports routes.
    from .routes import health as health_routes
    from .routes import reports as reports_routes
    app.include_router(health_routes.router)
    app.include_router(reports_routes.router)

    log.info("app-factory-done routes=%d", len(app.routes))
    return app


# Convenience for `uvicorn app.api.app_factory:app`
app = create_app()
