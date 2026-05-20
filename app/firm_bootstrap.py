"""Idempotent first-boot portfolio seed — policy-aligned with intentional gaps.

Derives weights from ``allocation.SECTOR_TARGETS`` and ``TARGET_INVESTED_PCT``
so the book starts inside policy bands with a small deploy/diversify gap for
the Portfolio Manager (not perfect 85% / 28% IT — room to route scans).

Typical outcome (before qty rounding):
  * 15–16 names (target band 10–16)
  * ~83–84% invested (target 85%, min 70%)
  * ~16–17% cash (above 8% cash target, below 20% ceiling)
  * Sectors ~93% of strategic targets; no name above 8% NAV cap
  * Trading posture ``balanced`` (not forced deploy/diversify)
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from . import allocation, bootstrap_data, config, db, trade_history
from .core.logging import get_logger

log = get_logger("horizon.bootstrap")

# Stay below 7% warn band so init posture is ``balanced``, not ``constrained``.
_MAX_SINGLE_NAME_PCT = allocation.position_warn_pct() - 0.005


def _holdings_spec() -> dict:
    return bootstrap_data.load_initial_holdings_spec()


def _distribute_to_target(
    weights: list[float],
    target: float,
    cap: float,
) -> list[float]:
    """Scale weights to ``target`` sum without exceeding ``cap`` per name."""
    out = list(weights)
    for _ in range(32):
        total = sum(out)
        if abs(total - target) < 1e-5:
            break
        if total <= 0:
            break
        if total < target:
            room = [cap - w for w in out]
            spare = target - total
            eligible = [i for i, r in enumerate(room) if r > 1e-8]
            if not eligible:
                break
            share = spare / len(eligible)
            for i in eligible:
                out[i] = min(cap, out[i] + min(share, room[i]))
        else:
            factor = target / total
            out = [min(cap, w * factor) for w in out]
    return [round(w, 4) for w in out]


def _build_bootstrap_book() -> list[tuple[str, str, float, float]]:
    """(ticker, sector, target_pct_nav, ref_price) from allocation policy."""
    spec = _holdings_spec()
    gap = float(spec.get("invested_gap_pct", 0.015))
    sector_ratio = float(spec.get("sector_target_ratio", 0.93))
    rows: list[tuple[str, str, float, float]] = []
    for sector, entries in (spec.get("sectors") or {}).items():
        tgt = allocation.SECTOR_TARGETS.get(sector, {}).get("target", 0.04)
        sleeve = tgt * sector_ratio
        n = len(entries) if entries else 0
        per_name = sleeve / n if n else 0.0
        for entry in entries:
            rows.append((
                str(entry["ticker"]).upper(),
                sector,
                per_name,
                float(entry["ref_price"]),
            ))

    target_invested = allocation.TARGET_INVESTED_PCT - gap
    raw = [pct for _, _, pct, _ in rows]
    adjusted = _distribute_to_target(raw, target_invested, _MAX_SINGLE_NAME_PCT)
    return [
        (ticker, sector, pct, ref_price)
        for (ticker, sector, _p, ref_price), pct in zip(rows, adjusted, strict=False)
    ]


_BOOTSTRAP_BOOK: list[tuple[str, str, float, float]] = _build_bootstrap_book()


def bootstrap_policy_snapshot() -> dict[str, Any]:
    """Expected book metrics from the static bootstrap table (pre-qty rounding)."""
    invested = sum(p for _, _, p, _ in _BOOTSTRAP_BOOK)
    by_sector: dict[str, float] = {}
    for _, sector, pct, _ in _BOOTSTRAP_BOOK:
        by_sector[sector] = by_sector.get(sector, 0.0) + pct
    return {
        "positions": len(_BOOTSTRAP_BOOK),
        "invested_pct": round(invested, 4),
        "cash_pct": round(1 - invested, 4),
        "sector_pct": {k: round(v, 4) for k, v in sorted(by_sector.items())},
    }


def _trim_seed_book_to_cash_target(nav: float) -> None:
    """Scale seed holdings down if marks imply cash below operating target."""
    gap = float(_holdings_spec().get("invested_gap_pct", 0.015))
    target_invested = allocation.TARGET_INVESTED_PCT - gap
    max_invested_usd = nav * target_invested
    holdings = db.list_holdings()
    total = sum(
        int(h.get("quantity") or 0) * float(h.get("current_price") or 0)
        for h in holdings
    )
    if total <= max_invested_usd + 1e-6:
        return
    scale = max_invested_usd / total if total else 1.0
    for h in holdings:
        ticker = h["ticker"]
        qty = int(h.get("quantity") or 0)
        if qty <= 0:
            continue
        price = float(h.get("current_price") or 0)
        new_mv = qty * price * scale
        if ticker in ("BTC", "ETH"):
            new_qty = 1
            new_price = new_mv
        else:
            new_qty = max(1, int(round(new_mv / price))) if price else qty
            new_price = new_mv / new_qty if new_qty else price
        plan_id = h.get("plan_id") or ""
        sector = h.get("sector") or "Unknown"
        db.upsert_holding(
            ticker, new_qty, float(h.get("cost_basis") or new_price),
            new_price, plan_id, sector,
        )


def _minimal_active_plan(ticker: str, sector: str, entry_price: float, pct_nav: float) -> dict:
    as_of = time.strftime("%Y-%m-%dT%H:%M:%S")
    plan_id = f"plan_seed_{ticker.lower()}_{uuid.uuid4().hex[:6]}"
    return {
        "id": plan_id,
        "ticker": ticker,
        "sector": sector,
        "created_at": as_of,
        "approved_at": as_of,
        "status": "active",
        "thesis": {
            "narrative": (
                f"Bootstrap position in {ticker} — seeded at firm init. "
                f"Meets sizing policy ({pct_nav:.1%} NAV); manager may route "
                f"add-on research or sector rebalance per trading posture."
            ),
            "supporting_points": [
                "Dossier or universe coverage on file",
                "Position within per-name cap (investment policy §2)",
                "Monitoring checks active per plan template",
            ],
            "valuation_target_range": {
                "metric": "price",
                "low": round(entry_price * 0.85, 2),
                "high": round(entry_price * 1.25, 2),
            },
            "expected_holding_horizon": "12-18 months",
        },
        "entry": {
            "side": "long",
            "target_size_pct_nav": pct_nav,
            "entry_type": "limit",
            "entry_price_or_trigger": {"type": "limit_price", "value": entry_price},
            "execution_window_days": 5,
        },
        "monitoring": {
            "interval": "daily",
            "checks": [
                {"name": "price_drift", "type": "price_drift",
                 "threshold_pct": -0.25, "on_breach": "trigger_re_eval"},
                {"name": "news_materiality", "type": "news_materiality",
                 "threshold": 0.7, "on_breach": "trigger_re_eval"},
            ],
        },
        "guardrails": {
            "soft_stop_loss_pct": -0.25,
            "hard_position_cap_pct_nav": allocation.MAX_POSITION_PCT,
            "time_stop": {"max_holding_period_months": 24, "action": "review"},
        },
        "exit": {
            "take_profit": {"metric": "valuation_band_high", "action": "review"},
            "stop_loss": {"metric": "soft_stop_loss_pct", "action": "trigger_re_eval"},
            "time_stop": {"max_holding_period_months": 24, "action": "review"},
        },
        "history": [{
            "at": as_of,
            "agent": "firm_bootstrap",
            "action": "seeded_active",
        }],
    }


def ensure_balanced_book() -> dict[str, Any]:
    """Seed holdings + active plans when the book is empty (first deploy)."""
    if db.list_holdings():
        return {"seeded": False, "reason": "holdings_exist", "positions": len(db.list_holdings())}

    nav = float(config.STARTING_NAV)
    as_of = time.strftime("%Y-%m-%dT%H:%M:%S")
    seeded: list[dict] = []
    sectors_seen: set[str] = set()
    digital_pct = 0.0

    for ticker, sector, pct, ref_price in _BOOTSTRAP_BOOK:
        notional = nav * pct
        if ticker in ("BTC", "ETH"):
            qty = 1
            fill_price = notional
        else:
            qty = max(1, int(round(notional / ref_price)))
            fill_price = notional / qty

        if sector == "Digital Assets":
            digital_pct += pct

        plan = _minimal_active_plan(ticker, sector, fill_price, pct)
        plan_id = plan["id"]
        db.save_plan(plan_id, ticker, "active", plan)
        db.upsert_holding(ticker, qty, fill_price, fill_price, plan_id, sector)
        sectors_seen.add(sector)

        trade_history.record_trade(
            ticker=ticker,
            action="open",
            side="long",
            quantity=qty,
            price=fill_price,
            notional_usd=qty * fill_price,
            plan_id=plan_id,
            run_id="bootstrap",
            sector=sector,
            source="bootstrap",
            as_of=as_of,
            meta={"seed": True, "pct_nav": pct},
            trade_id=f"bootstrap_open_{ticker}",
        )
        seeded.append({
            "ticker": ticker,
            "sector": sector,
            "pct_nav": pct,
            "qty": qty,
            "price": fill_price,
        })

    _trim_seed_book_to_cash_target(nav)

    invested_pct = sum(r["pct_nav"] for r in seeded)
    lo, hi = allocation.TARGET_POSITION_COUNT
    policy = bootstrap_policy_snapshot()
    log.info(
        "firm-bootstrap-done positions=%d sectors=%d invested_pct=%.1f cash_pct=%.1f "
        "target_invested=%.1f gap=%.1fpp",
        len(seeded),
        len(sectors_seen),
        invested_pct * 100,
        (1 - invested_pct) * 100,
        allocation.TARGET_INVESTED_PCT * 100,
        (allocation.TARGET_INVESTED_PCT - invested_pct) * 100,
    )
    return {
        "seeded": True,
        "positions": len(seeded),
        "sectors": len(sectors_seen),
        "invested_pct": round(invested_pct, 4),
        "cash_pct": round(1 - invested_pct, 4),
        "tickers": [s["ticker"] for s in seeded],
        "digital_assets_pct": round(digital_pct, 4),
        "policy_target": policy,
        "within_position_count_band": lo <= len(seeded) <= hi,
    }
