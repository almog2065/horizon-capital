"""Manager + firm-state adjustments for Idea Generator ranking."""
from __future__ import annotations

from typing import Any, Optional

from . import allocation, asset_universe
from .agents import firm_manager


def firm_book_score(
    firm_state: Optional[dict],
    sector: str,
    ticker: str,
) -> float:
    """0–1 score from deployment needs and sector bands (independent of manager)."""
    if not firm_state:
        return 0.5
    from . import firm_state as fs_mod

    sec = allocation.normalize_sector(sector)
    deploy = fs_mod.deployment_needs(firm_state)
    score = 0.45
    if deploy.get("active"):
        score += 0.15
    for row in firm_state.get("sectors") or []:
        if row["sector"] != sec:
            continue
        pct = float(row.get("pct_nav") or 0)
        low = float(row.get("band_low_pct") or 0)
        tgt = float(row.get("target_pct") or 0)
        if pct < low - 0.005:
            score += 0.25 + min(0.15, (low - pct) * 2)
        elif pct < tgt:
            score += 0.10
        elif pct > float(row.get("band_high_pct") or 0.25):
            score -= 0.20
        break
    held = ticker.upper() in set(firm_state.get("holdings_tickers") or [])
    if held and not deploy.get("need_diversify"):
        score -= 0.05
    meta = asset_universe.resolve(ticker)
    if deploy.get("need_diversify") and meta.asset_class in (
        "commodity_proxy", "rates_proxy", "fx_proxy", "crypto",
    ):
        score += 0.08
    return max(0.0, min(1.0, score))


def candidate_adjustment(
    ticker: str,
    sector: str,
    manager_out: Optional[dict],
    firm_state: Optional[dict],
    *,
    deploy_mode: bool = False,
    below_band: bool = False,
) -> dict[str, float]:
    """Composite / threshold deltas from manager directives + book state."""
    out = {
        "composite_boost": 0.0,
        "fit_boost": 0.0,
        "open_threshold_delta": 0.0,
    }
    if not manager_out and not firm_state:
        return out

    sec = allocation.normalize_sector(sector)
    t = ticker.upper()

    out["composite_boost"] += firm_manager.sector_score_adjustment(sec, manager_out)
    out["composite_boost"] += firm_manager.ticker_score_adjustment(t, manager_out)

    if firm_state:
        book = firm_book_score(firm_state, sec, t)
        out["composite_boost"] += (book - 0.5) * 0.20

    sd = (manager_out or {}).get("scan_directives") or {}
    if deploy_mode or sd.get("deploy_urgency") in ("high", "critical"):
        out["open_threshold_delta"] -= 0.06
        if below_band:
            out["composite_boost"] += 0.08
            out["open_threshold_delta"] -= 0.04

    for row in sd.get("bias_asset_classes") or []:
        ac = row.get("asset_class", "")
        boost = float(row.get("boost") or 0.06)
        meta = asset_universe.resolve(t)
        if meta.asset_class == ac:
            out["composite_boost"] += boost

    out["composite_boost"] = max(-0.20, min(0.25, out["composite_boost"]))
    out["fit_boost"] = max(0.0, min(0.15, out["composite_boost"] * 0.5))
    out["open_threshold_delta"] = max(-0.12, min(0.0, out["open_threshold_delta"]))
    return out
