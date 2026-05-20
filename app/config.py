"""Backwards-compat shim over `app.core.settings`.

The legacy code base imports module-level constants from `app.config`
(e.g. `from app import config; config.FIRM_DB`). Rather than touch every
call site, this module re-exports the production settings as module
attributes. New code SHOULD import from `app.core.settings` directly:

    from app.core.settings import get_settings
    cfg = get_settings()

All values here are read from the pydantic `Settings` singleton — there
is exactly one source of truth.
"""
from __future__ import annotations

from pathlib import Path

from .core.logging import get_logger
from .core.settings import REPO_ROOT, get_settings

_log = get_logger("horizon.config")
_settings = get_settings()

# ---- mirror legacy names so existing imports keep working ----
OPENAI_API_KEY: str = _settings.OPENAI_API_KEY
OPENAI_MODEL: str = _settings.OPENAI_MODEL
OPENAI_EMBEDDING_MODEL: str = _settings.OPENAI_EMBEDDING_MODEL
USE_MOCK_LLM: bool = _settings.derived_mock()

ROOT: Path = REPO_ROOT
ARTIFACTS: Path = _settings.artifacts_dir()
FIRM_DB: Path = _settings.FIRM_DB
VECTOR_DB: Path = _settings.VECTOR_DB
OPS_DB: Path = _settings.OPS_DB
CHECKPOINT_DB: Path = _settings.CHECKPOINT_DB

DATA: Path = ROOT / "data"
POLICIES_DIR: Path = DATA / "policies"
DOSSIERS_SEED_DIR: Path = DATA / "dossiers"
# Runtime dossiers live in ops.sqlite; kept for diagnostics/back-compat labels.
DOSSIERS_RUNTIME_DIR: Path = _settings.OPS_DB
# Backwards-compat: seed path (read). Use dossier_paths for reads/writes.
DOSSIERS_DIR: Path = DOSSIERS_SEED_DIR
PAST_PLANS_DIR: Path = DATA / "past_plans"
NEWS_SAMPLES_DIR: Path = DATA / "news_samples"

STARTING_NAV: float = _settings.STARTING_NAV

AUTO_PLAN_SUPERVISION: bool = _settings.AUTO_PLAN_SUPERVISION
AUTO_PLAN_EXECUTE: bool = _settings.AUTO_PLAN_EXECUTE
AUTO_PLAN_SPAWN_PIPELINE: bool = _settings.AUTO_PLAN_SPAWN_PIPELINE
PLAN_SUPERVISION_INTERVAL_SEC: int = _settings.PLAN_SUPERVISION_INTERVAL_SEC

HITL_ONE_PER_TICKER: bool = _settings.HITL_ONE_PER_TICKER
BLOCK_DUPLICATE_PIPELINE: bool = _settings.BLOCK_DUPLICATE_PIPELINE
BLOCK_PIPELINE_IF_PENDING_HITL: bool = _settings.BLOCK_PIPELINE_IF_PENDING_HITL
HITL_MAIDEN_ONLY: bool = _settings.HITL_MAIDEN_ONLY
HITL_OPEN_POSITION_AGENT_DECIDES: bool = _settings.HITL_OPEN_POSITION_AGENT_DECIDES

FIRM_MANAGER_AUTO_TRIGGER: bool = _settings.FIRM_MANAGER_AUTO_TRIGGER
FIRM_MANAGER_MAX_TRIGGERS_PER_CYCLE: int = _settings.FIRM_MANAGER_MAX_TRIGGERS_PER_CYCLE
FIRM_MANAGER_SCAN_COOLDOWN_SEC: int = _settings.FIRM_MANAGER_SCAN_COOLDOWN_SEC
FIRM_MANAGER_BALANCE_FORCE_SCAN: bool = _settings.FIRM_MANAGER_BALANCE_FORCE_SCAN
FIRM_MANAGER_SCAN_TOP_K: int = _settings.FIRM_MANAGER_SCAN_TOP_K
FIRM_BALANCE_INTERVAL_SEC: int = _settings.FIRM_BALANCE_INTERVAL_SEC

MARKET_CADENCE_ENABLED: bool = _settings.MARKET_CADENCE_ENABLED
CADENCE_MARKET_OPEN_AUTO_EXECUTE: bool = _settings.CADENCE_MARKET_OPEN_AUTO_EXECUTE
SKIP_MARKET_HOURS_CADENCE: bool = _settings.SKIP_MARKET_HOURS_CADENCE

# Multi-asset / market-data discovery knobs (added with the multi-asset update).
ENABLE_COINGECKO: bool = _settings.ENABLE_COINGECKO
SCAN_MAX_EVALUATE: int = _settings.SCAN_MAX_EVALUATE
SCAN_API_DISCOVERY_COUNT: int = _settings.SCAN_API_DISCOVERY_COUNT
ENABLE_FX_CONTEXT: bool = _settings.ENABLE_FX_CONTEXT
MANAGER_BOOK_SCORE_WEIGHT: float = _settings.MANAGER_BOOK_SCORE_WEIGHT

_log.info(
    "config-loaded",
    extra={
        "event": "config",
        "env": _settings.APP_ENV,
        "llm_mode": "mock" if USE_MOCK_LLM else "live",
        "firm_db": str(FIRM_DB),
        "ops_db": str(OPS_DB),
    },
)
