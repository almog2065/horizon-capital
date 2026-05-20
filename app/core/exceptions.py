"""Global exception handling — user-visible alerts + structured API errors."""
from __future__ import annotations

import traceback
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..ops_alerts import record
from .logging import get_logger

log = get_logger("horizon.errors")


def _wants_json(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept and "application/json" not in accept:
        return False
    if request.url.path.startswith("/api/"):
        return True
    return "application/json" in accept


def _error_body(
    *,
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
    request_id: str = "",
) -> dict:
    body: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "status": status_code,
        },
    }
    if detail is not None:
        body["error"]["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse | HTMLResponse:
        rid = getattr(request.state, "request_id", "")
        detail = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
        severity = "warning" if exc.status_code < 500 else "error"
        record(
            code=f"http_{exc.status_code}",
            message=message,
            severity=severity,
            source=request.url.path,
            context={"status_code": exc.status_code, "request_id": rid},
        )
        body = _error_body(
            status_code=exc.status_code,
            code=f"http_{exc.status_code}",
            message=message,
            detail=detail if not isinstance(detail, str) else None,
            request_id=rid,
        )
        if _wants_json(request):
            return JSONResponse(body, status_code=exc.status_code)
        html = (
            f"<h1>Error {exc.status_code}</h1>"
            f"<p>{message}</p>"
            f"<p><a href='/'>Dashboard</a> · <a href='/alerts'>Alerts</a></p>"
        )
        return HTMLResponse(html, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse | HTMLResponse:
        rid = getattr(request.state, "request_id", "")
        tb = traceback.format_exc()
        log.exception(
            "unhandled-exception path=%s rid=%s",
            request.url.path,
            rid,
        )
        record(
            code="unhandled_exception",
            message=str(exc) or exc.__class__.__name__,
            severity="critical",
            source=request.url.path,
            context={
                "request_id": rid,
                "exception_type": exc.__class__.__name__,
                "traceback": tb,
                "method": request.method,
                "path": request.url.path,
            },
        )
        body = _error_body(
            status_code=500,
            code="internal_error",
            message="An unexpected error occurred. The operations team has been notified.",
            request_id=rid,
        )
        if _wants_json(request):
            return JSONResponse(body, status_code=500)
        html = (
            "<h1>Internal error</h1>"
            "<p>Something went wrong. Check <a href='/alerts'>Alerts</a> for details "
            "or <a href='/diagnostics'>Diagnostics</a>.</p>"
            f"<p class='muted'>Request ID: {rid}</p>"
        )
        return HTMLResponse(html, status_code=500)
