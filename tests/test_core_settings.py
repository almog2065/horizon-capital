"""Smoke tests for the production settings layer."""
from __future__ import annotations


def test_settings_defaults_load():
    from app.core import settings as s

    cfg = s.get_settings()
    assert cfg.APP_NAME == "horizon-capital"
    assert cfg.HTTP_PORT == 8000
    # mock derivation: no key + no flag => mock-on
    if not cfg.OPENAI_API_KEY:
        assert cfg.derived_mock() is True


def test_config_shim_reexports():
    from app import config

    # the legacy code imports these by name — they must still exist
    for attr in (
        "FIRM_DB",
        "VECTOR_DB",
        "OPENAI_MODEL",
        "STARTING_NAV",
        "AUTO_PLAN_SUPERVISION",
        "POLICIES_DIR",
    ):
        assert hasattr(config, attr), f"app.config missing {attr}"


def test_logging_setup_idempotent():
    from app.core import logging as log_mod

    log_mod.setup_logging(force=True)
    log_mod.setup_logging()  # second call must be a no-op
    logger = log_mod.get_logger("test")
    logger.info("hello-test")  # must not raise


def test_health_module_runs():
    from app.core import health

    out = health.liveness()
    assert out["status"] == "ok"
    deep = health.readiness()
    assert deep["status"] in ("ok", "degraded", "fail")
