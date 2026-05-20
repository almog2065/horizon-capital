"""Production settings module.

Pydantic-based, single source of truth for runtime configuration. All
other modules SHOULD import `settings` from here rather than calling
`os.getenv` directly. Backwards-compat shims in `app.config` re-export
the values so legacy imports continue to work.

Configuration precedence (highest -> lowest):
    1. Real process env (12-factor)
    2. .env file pointed to by ENV_FILE
    3. /run/secrets/<key> mounted files (Docker secrets)
    4. ./.env (repo root)
    5. Defaults defined below

Why pydantic-settings: typed defaults, validation at startup, and clean
overrides in tests. Defaults are tuned so a freshly-cloned repo runs
out-of-the-box in MOCK mode with SQLite storage; production overrides
are supplied via environment.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

try:
    from pydantic import Field, field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover - fallback when pydantic-settings missing
    _HAS_PYDANTIC_SETTINGS = False
    from pydantic import BaseModel as _BaseSettings  # type: ignore

    class BaseSettings(_BaseSettings):  # type: ignore[no-redef]
        model_config: dict = {}

    def Field(default=None, **kwargs):  # type: ignore[no-redef]
        return default

    def field_validator(*_, **__):  # type: ignore[no-redef]
        def decorator(fn):
            return fn
        return decorator

    class SettingsConfigDict(dict):  # type: ignore[no-redef]
        pass


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_docker_secret(name: str) -> Optional[str]:
    """Read a Docker secret if mounted at /run/secrets/<name>."""
    candidate = Path("/run/secrets") / name
    try:
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return None


class Settings(BaseSettings):
    """Runtime settings.

    All fields can be overridden by env vars with matching names.
    """

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- App identity -----
    APP_NAME: str = "horizon-capital"
    APP_ENV: Literal["dev", "staging", "prod", "test"] = "dev"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "text"] = "text"

    # ----- HTTP server -----
    HTTP_HOST: str = "0.0.0.0"
    HTTP_PORT: int = 8000
    UVICORN_WORKERS: int = 1  # >1 only without in-process schedulers
    UVICORN_RELOAD: bool = False

    # ----- LLM -----
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    USE_MOCK_LLM: bool = False  # auto-true when no key

    # ----- LangSmith observability -----
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "horizon-capital"
    LANGSMITH_TRACING: bool = True

    # ----- Persistence -----
    # Backwards-compat: paths used by the existing sqlite-based modules.
    FIRM_DB: Path = REPO_ROOT / "artifacts" / "firm.sqlite"
    VECTOR_DB: Path = REPO_ROOT / "artifacts" / "vectors.sqlite"
    OPS_DB: Path = REPO_ROOT / "artifacts" / "ops.sqlite"
    CHECKPOINT_DB: Path = REPO_ROOT / "artifacts" / "checkpoints.sqlite"

    # Optional production-grade backends. When set, services will prefer
    # these over the local sqlite files.
    DATABASE_URL: str = ""      # e.g. postgresql+psycopg://user:pw@db:5432/horizon
    REDIS_URL: str = ""          # e.g. redis://redis:6379/0

    # ----- Worker / scheduler split -----
    WORKER_METRICS_PORT: int = 9091
    RUN_SCHEDULER_IN_API: bool = True
    AUTO_PLAN_SUPERVISION: bool = True
    AUTO_PLAN_EXECUTE: bool = False
    AUTO_PLAN_SPAWN_PIPELINE: bool = True
    PLAN_SUPERVISION_INTERVAL_SEC: int = 1800
    FIRM_BALANCE_INTERVAL_SEC: int = 0

    # ----- Market-hours cadence (operating-cadence §1) -----
    MARKET_CADENCE_ENABLED: bool = True
    # Run open-job auto-execution (HITL thresholds still apply).
    CADENCE_MARKET_OPEN_AUTO_EXECUTE: bool = False
    # Dev/replay: fire cadence jobs outside 09:30–16:00 ET.
    SKIP_MARKET_HOURS_CADENCE: bool = False

    # ----- Firm policy knobs -----
    STARTING_NAV: float = 1_000_000.0
    HITL_ONE_PER_TICKER: bool = True
    BLOCK_DUPLICATE_PIPELINE: bool = True
    BLOCK_PIPELINE_IF_PENDING_HITL: bool = True
    HITL_MAIDEN_ONLY: bool = True
    HITL_OPEN_POSITION_AGENT_DECIDES: bool = True
    FIRM_MANAGER_AUTO_TRIGGER: bool = True
    FIRM_MANAGER_MAX_TRIGGERS_PER_CYCLE: int = 4
    FIRM_MANAGER_SCAN_COOLDOWN_SEC: int = 3600
    FIRM_MANAGER_BALANCE_FORCE_SCAN: bool = True
    FIRM_MANAGER_SCAN_TOP_K: int = 3

    # ----- Multi-asset / market data discovery -----
    # CoinGecko (public, no key) — enables crypto dossiers (BTC, ETH, ...).
    ENABLE_COINGECKO: bool = True
    # Throttles for idea-scan API discovery.
    SCAN_MAX_EVALUATE: int = 24             # max names per mechanical-screen pass
    SCAN_API_DISCOVERY_COUNT: int = 20      # EDGAR discovery batch size
    # Frankfurter FX (public, no key) for macro / FX context blocks.
    ENABLE_FX_CONTEXT: bool = True
    # MCP-equivalent market data (in-process; no paid keys). See docs/mcp-market-data.md.
    MCP_MARKET_ENABLED: bool = True
    MCP_COINGECKO_TRENDING: bool = True   # trending coins in idea discovery
    MCP_YFINANCE_HISTORY: bool = True     # OHLCV via yfinance for agents
    # Idea Generator: weight of firm-book + portfolio-manager signals in the
    # composite score (0..1). Higher = trust manager more.
    MANAGER_BOOK_SCORE_WEIGHT: float = 0.12

    # ----- Security -----
    ALLOWED_HOSTS: str = "*"
    CORS_ORIGINS: str = ""
    REQUEST_TIMEOUT_SEC: int = 60

    # ----- Healthcheck -----
    HEALTH_DB_TIMEOUT_SEC: float = 2.0

    # --------------------------------------------------------------
    # Validators
    # --------------------------------------------------------------
    @field_validator("OPENAI_API_KEY", mode="before")
    @classmethod
    def _maybe_docker_secret_openai(cls, v):
        if not v:
            return _read_docker_secret("openai_api_key") or ""
        return v

    @field_validator("LANGSMITH_API_KEY", mode="before")
    @classmethod
    def _maybe_docker_secret_langsmith(cls, v):
        if not v:
            return _read_docker_secret("langsmith_api_key") or ""
        return v

    @field_validator("USE_MOCK_LLM", mode="before")
    @classmethod
    def _coerce_mock_llm(cls, v):
        # accept "1"/"true"/"yes" strings
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return v

    def derived_mock(self) -> bool:
        """LLM is in mock mode if explicitly set OR no key available."""
        return bool(self.USE_MOCK_LLM) or not self.OPENAI_API_KEY.strip()

    def artifacts_dir(self) -> Path:
        p = self.FIRM_DB.parent
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use this everywhere."""
    s = Settings()
    # ensure dirs exist for SQLite backends
    s.artifacts_dir()
    return s


# Re-export a module-level singleton for convenience.
settings = get_settings()
