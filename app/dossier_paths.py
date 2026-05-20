"""Dossier storage: bootstrap seed in ``data/dossiers``, runtime in ``ops.sqlite``."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import config, ops_db


def seed_dir() -> Path:
    """Bundled dossiers shipped with the image (read-only at setup)."""
    return config.DOSSIERS_SEED_DIR


def resolve_read_path(ticker: str) -> Optional[Path]:
    """Filesystem path only for seed fallback (runtime lives in DB)."""
    t = ticker.upper()
    hit = ops_db.get_dossier(t)
    if hit:
        return Path(f"ops://dossiers/{t}")
    for ext in (".json", ".yaml"):
        p = seed_dir() / f"{t}{ext}"
        if p.exists():
            return p
    return None


def write_path(ticker: str) -> Path:
    return Path(f"ops://dossiers/{ticker.upper()}.json")


def load(ticker: str) -> dict[str, Any]:
    ops_db.init_db()
    t = ticker.upper()
    hit = ops_db.get_dossier(t)
    if hit:
        return {
            "found": True,
            "dossier": hit["dossier"],
            "source_path": f"ops.sqlite:{hit['source']}",
        }
    for ext in (".json", ".yaml"):
        p = seed_dir() / f"{t}{ext}"
        if p.exists():
            if ext == ".json":
                return {
                    "found": True,
                    "dossier": json.loads(p.read_text(encoding="utf-8")),
                    "source_path": str(p),
                }
            return {
                "found": True,
                "dossier_yaml_raw": p.read_text(encoding="utf-8"),
                "source_path": str(p),
            }
    return {"found": False, "ticker": t, "not_found_reason": "no_dossier"}


def save(ticker: str, dossier: dict) -> Path:
    ops_db.init_db()
    ops_db.upsert_dossier(ticker, dossier, source="runtime")
    return write_path(ticker)


def list_tickers() -> list[str]:
    ops_db.init_db()
    seen = set(ops_db.list_dossier_tickers())
    if seed_dir().exists():
        for p in seed_dir().glob("*.json"):
            seen.add(p.stem.upper())
    return sorted(seen)
