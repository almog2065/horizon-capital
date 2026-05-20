"""Shared pytest fixtures.

We don't want pydantic / pydantic-settings to be hard requirements for
running the eval / reporter tests. When those packages aren't present
(e.g., a minimal sandbox), we stub the two modules they back so the
rest of the package still imports.
"""
from __future__ import annotations

import sys
import types
import logging


def _maybe_stub_pydantic_backed_modules() -> None:
    try:
        import pydantic_settings  # noqa: F401
        return  # real pydantic-settings is available — no stubbing needed
    except ImportError:
        pass

    # Stub app.core.logging (real one imports settings which imports pydantic-settings)
    log_mod = types.ModuleType("app.core.logging")
    log_mod.setup_logging = lambda force=False: logging.basicConfig(level=logging.INFO)
    log_mod.get_logger = logging.getLogger
    sys.modules["app.core.logging"] = log_mod

    # Stub app.core.settings minimally
    settings_mod = types.ModuleType("app.core.settings")

    import pathlib
    _REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

    class _StubSettings:
        APP_NAME = "horizon-capital"
        APP_ENV = "test"
        HTTP_PORT = 8000
        OPENAI_API_KEY = ""
        OPENAI_MODEL = "gpt-4o-mini"
        OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
        FIRM_DB = _REPO_ROOT / "artifacts" / "firm.sqlite"
        VECTOR_DB = _REPO_ROOT / "artifacts" / "vectors.sqlite"
        OPS_DB = _REPO_ROOT / "artifacts" / "ops.sqlite"
        CHECKPOINT_DB = _REPO_ROOT / "artifacts" / "checkpoints.sqlite"
        STARTING_NAV = 1_000_000.0
        # All the boolean / int knobs config.py reads:
        AUTO_PLAN_SUPERVISION = True
        AUTO_PLAN_EXECUTE = False
        AUTO_PLAN_SPAWN_PIPELINE = True
        PLAN_SUPERVISION_INTERVAL_SEC = 1800
        HITL_ONE_PER_TICKER = True
        BLOCK_DUPLICATE_PIPELINE = True
        BLOCK_PIPELINE_IF_PENDING_HITL = True
        HITL_MAIDEN_ONLY = True
        HITL_OPEN_POSITION_AGENT_DECIDES = True
        FIRM_MANAGER_AUTO_TRIGGER = True
        FIRM_MANAGER_MAX_TRIGGERS_PER_CYCLE = 4
        FIRM_MANAGER_SCAN_COOLDOWN_SEC = 3600
        FIRM_MANAGER_BALANCE_FORCE_SCAN = True
        FIRM_MANAGER_SCAN_TOP_K = 3
        FIRM_BALANCE_INTERVAL_SEC = 0
        ENABLE_COINGECKO = True
        ENABLE_FX_CONTEXT = True
        SCAN_MAX_EVALUATE = 24
        SCAN_API_DISCOVERY_COUNT = 20
        MANAGER_BOOK_SCORE_WEIGHT = 0.12

        def derived_mock(self) -> bool:
            return True

        def artifacts_dir(self):
            import tempfile
            return pathlib.Path(tempfile.mkdtemp(prefix="horizon-art-"))

    settings_mod.get_settings = lambda: _StubSettings()
    settings_mod.settings = _StubSettings()
    settings_mod.REPO_ROOT = _REPO_ROOT
    sys.modules["app.core.settings"] = settings_mod


_maybe_stub_pydantic_backed_modules()
