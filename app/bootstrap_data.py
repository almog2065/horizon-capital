"""Load first-boot reference data from ``data/bootstrap/`` into databases.

Runtime mutations go to SQLite only; repo files are read once at setup.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import config, ops_db
from .core.logging import get_logger

log = get_logger("horizon.bootstrap_data")

_BOOTSTRAP_DIR = config.DATA / "bootstrap"


def _read_json(name: str) -> Any:
    path = _BOOTSTRAP_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"bootstrap file missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_initial_holdings_spec() -> dict[str, Any]:
    """Sector sleeves + ref prices for first portfolio seed."""
    return _read_json("initial_holdings.json")


def load_synthetic_filings() -> dict[str, list[str]]:
    return _read_json("synthetic_filings.json")


def load_news_seeds() -> list[dict[str, Any]]:
    return _read_json("news_seeds.json")


def ensure_dossiers_seeded() -> dict[str, Any]:
    """Copy bundled ``data/dossiers`` into ops DB when empty."""
    ops_db.init_db()
    if ops_db.bootstrap_done("dossiers_seed"):
        return {"seeded": False, "reason": "already_done", "count": ops_db.count_dossiers()}

    seed_dir = config.DOSSIERS_SEED_DIR
    if not seed_dir.exists():
        ops_db.mark_bootstrap_done("dossiers_seed", {"count": 0})
        return {"seeded": False, "reason": "no_seed_dir"}

    n = 0
    for path in sorted(seed_dir.glob("*.json")):
        dossier = json.loads(path.read_text(encoding="utf-8"))
        ops_db.upsert_dossier(path.stem, dossier, source="bootstrap")
        n += 1

    ops_db.mark_bootstrap_done("dossiers_seed", {"count": n, "dir": str(seed_dir)})
    log.info("dossiers-seeded count=%d from %s", n, seed_dir)
    return {"seeded": n > 0, "count": n}


def ensure_runtime_bootstrap() -> dict[str, Any]:
    """Idempotent startup: init ops DB, migrate legacy JSON, seed dossiers."""
    ops_db.init_db()
    migrated = ops_db.migrate_legacy_json_artifacts()
    dossiers = ensure_dossiers_seeded()
    return {"migrated": migrated, "dossiers": dossiers}
