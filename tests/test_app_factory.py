"""Importability check for the new app factory + routes.

We don't spin up uvicorn — we just verify the FastAPI app object is
importable and exposes the new ops endpoints.
"""
from __future__ import annotations


def test_app_factory_exposes_health_routes():
    from app.api.app_factory import app

    paths = {getattr(r, "path", None) for r in app.routes}
    for required in ("/healthz", "/readyz", "/version", "/metrics"):
        assert required in paths, f"missing route {required}"


def test_legacy_main_app_still_importable():
    # The legacy UI lives under app.main; the factory composes onto it.
    from app import main as legacy

    assert legacy.app is not None
    assert any(getattr(r, "path", None) == "/" for r in legacy.app.routes)
