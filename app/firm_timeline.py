"""Firm state timeline — metrics and events for dashboard charts."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import allocation, config, db, firm_state, trade_history


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except (ValueError, TypeError, OSError):
        return "—"


def _fmt_range(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (ValueError, TypeError, OSError):
        return "—"


def _metrics_from_firm_state(
    fs: dict,
    *,
    ts: float,
    source: str,
    run_id: str = "",
) -> dict[str, Any]:
    nav = float(fs.get("nav_usd") or 0)
    return {
        "ts": ts,
        "label": _fmt_ts(ts),
        "nav_usd": round(nav, 2),
        "cash_pct": round(float(fs.get("cash_pct") or 0) * 100, 2),
        "invested_pct": round(float(fs.get("invested_pct") or 0) * 100, 2),
        "positions_count": int(fs.get("positions_count") or 0),
        "posture": (fs.get("trading_posture") or {}).get("label")
        or (fs.get("trading_posture") or {}).get("posture"),
        "source": source,
        "run_id": run_id,
    }


def _metrics_from_daily_report(
    dr: dict,
    *,
    ts: float,
    source: str,
    run_id: str = "",
) -> Optional[dict[str, Any]]:
    nav = dr.get("nav")
    if nav is None:
        return None
    holdings = dr.get("holdings") or []
    invested = sum(float(h.get("market_value") or h.get("value") or 0) for h in holdings)
    nav_f = float(nav)
    invested_pct = (invested / nav_f * 100) if nav_f else None
    cash_pct = (100.0 - invested_pct) if invested_pct is not None else None
    return {
        "ts": ts,
        "label": _fmt_ts(ts),
        "nav_usd": round(nav_f, 2),
        "cash_pct": round(cash_pct, 2) if cash_pct is not None else None,
        "invested_pct": round(invested_pct, 2) if invested_pct is not None else None,
        "positions_count": len(holdings) if holdings else None,
        "posture": None,
        "source": source,
        "run_id": run_id,
    }


def _extract_run_metrics(row: dict, state: dict) -> Optional[dict[str, Any]]:
    ts = float(row.get("created_at") or time.time())
    run_id = row.get("run_id") or ""
    trigger = row.get("trigger_type") or ""

    fs = state.get("firm_state")
    if isinstance(fs, dict) and fs.get("nav_usd"):
        return _metrics_from_firm_state(fs, ts=ts, source=trigger, run_id=run_id)

    dr = state.get("daily_report")
    if isinstance(dr, dict):
        pt = _metrics_from_daily_report(dr, ts=ts, source=trigger or "daily_report", run_id=run_id)
        if pt:
            return pt

    mgr = state.get("firm_manager") or {}
    snap = mgr.get("firm_snapshot") if isinstance(mgr, dict) else None
    if isinstance(snap, dict) and snap.get("cash_pct") is not None:
        # Partial snapshot (no NAV) — skip for NAV series; still useful rarely
        return None
    return None


def _run_event(row: dict, state: dict) -> dict[str, Any]:
    trigger = row.get("trigger_type") or "run"
    status = row.get("status") or ""
    ts = float(row.get("created_at") or time.time())
    labels = {
        "firm_balance": "Policy routing",
        "cadence_eod": "EOD reconciliation",
        "idea_scan": "Idea scan",
        "plan_supervision": "Plan supervision",
        "news_event": "News pipeline",
    }
    label = labels.get(trigger, trigger.replace("_", " ").title())
    detail = status
    fs = state.get("final_status") or state.get("firm_manager", {}).get("book_summary")
    if isinstance(fs, str):
        detail = f"{status} — {fs[:80]}"
    elif state.get("spawned_run_ids"):
        detail = f"{status} · {len(state['spawned_run_ids'])} spawned"
    return {
        "ts": ts,
        "kind": "run",
        "label": label,
        "detail": str(detail)[:120],
        "run_id": row.get("run_id"),
        "ticker": "",
    }


def _trade_event(trade: dict) -> dict[str, Any]:
    action = trade.get("action") or ""
    ticker = (trade.get("ticker") or "").upper()
    qty = trade.get("quantity") or 0
    price = trade.get("price") or 0
    labels = {
        "buy": "Buy",
        "sell": "Sell",
        "open": "Open",
        "close": "Close",
    }
    label = f"{labels.get(action, action.title())} {ticker}"
    if qty and price:
        detail = f"{qty} @ ${float(price):,.2f}"
    elif trade.get("notional_usd"):
        detail = f"${float(trade['notional_usd']):,.0f}"
    else:
        detail = trade.get("source") or ""
    return {
        "ts": float(trade.get("ts") or time.time()),
        "kind": "trade",
        "label": label,
        "detail": detail,
        "run_id": trade.get("run_id") or "",
        "ticker": ticker,
    }


def _hitl_event(item: dict) -> dict[str, Any]:
    ts = float(item.get("resolved_at") or item.get("created_at") or time.time())
    resolution = item.get("resolution") or "resolved"
    plan_id = item.get("plan_id") or ""
    ticker = ""
    if plan_id:
        row = db.get_plan(plan_id)
        if row:
            ticker = (row.get("ticker") or "").upper()
    return {
        "ts": ts,
        "kind": "hitl",
        "label": f"HITL {resolution}",
        "detail": ticker or plan_id[:12],
        "run_id": item.get("run_id") or "",
        "ticker": ticker,
    }


def _dedupe_metrics(points: list[dict]) -> list[dict]:
    """Keep one point per minute bucket (latest wins)."""
    by_bucket: dict[int, dict] = {}
    for p in points:
        bucket = int(p["ts"] // 60)
        by_bucket[bucket] = p
    return sorted(by_bucket.values(), key=lambda x: x["ts"])


def build_firm_timeline(
    *,
    max_runs: int = 100,
    max_trades: int = 80,
    max_hitl: int = 40,
    include_current: bool = True,
) -> dict[str, Any]:
    """Build chart-ready series from runs, trades, and HITL."""
    trade_history.ensure_trade_history_seeded()

    metrics: list[dict] = []
    events: list[dict] = []

    run_rows = list(reversed(db.list_runs(max_runs)))
    for row in run_rows:
        full = db.get_run(row["run_id"])
        if not full:
            continue
        try:
            state = json.loads(full.get("state_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            state = {}
        pt = _extract_run_metrics(row, state)
        if pt:
            metrics.append(pt)
        events.append(_run_event(row, state))

    trades_chrono = list(reversed(db.list_trade_history(limit=max_trades)))
    for trade in trades_chrono:
        events.append(_trade_event(trade))
    first_trade_ts: Optional[float] = None
    if trades_chrono:
        first_trade_ts = min(float(t.get("ts") or 0) for t in trades_chrono)

    for item in db.list_hitl_recent(max_hitl):
        events.append(_hitl_event(item))

    if include_current:
        try:
            fs = firm_state.build_firm_state(refresh_prices=False)
            now = time.time()
            metrics.append(
                _metrics_from_firm_state(fs, ts=now, source="current", run_id=""),
            )
        except Exception:
            pass

    metrics = _dedupe_metrics(metrics)
    metrics.sort(key=lambda m: m["ts"])

    # Anchor NAV series at starting capital before first activity when possible.
    anchor_ts: Optional[float] = None
    if first_trade_ts:
        anchor_ts = first_trade_ts - 60
    elif metrics:
        anchor_ts = metrics[0]["ts"] - 3600
    if anchor_ts and anchor_ts > 0:
        if not metrics or metrics[0]["ts"] > anchor_ts + 30:
            metrics.insert(
                0,
                {
                    "ts": anchor_ts,
                    "label": _fmt_ts(anchor_ts),
                    "nav_usd": round(float(config.STARTING_NAV), 2),
                    "cash_pct": round(allocation.CASH_TARGET_PCT * 100, 2),
                    "invested_pct": round(allocation.TARGET_INVESTED_PCT * 100, 2),
                    "positions_count": 0,
                    "posture": "baseline",
                    "source": "starting_capital",
                    "run_id": "",
                },
            )

    events.sort(key=lambda e: e["ts"])

    all_ts = [m["ts"] for m in metrics] + [e["ts"] for e in events]
    now_ts = time.time()
    range_start_ts = min(all_ts) if all_ts else now_ts
    range_end_ts = max(all_ts + [now_ts]) if all_ts else now_ts
    if first_trade_ts and first_trade_ts < range_start_ts + 120:
        range_description = "From first recorded trade through now"
        anchored_on = "first_trade"
    elif metrics:
        range_description = "From first firm snapshot through now"
        anchored_on = "first_snapshot"
    else:
        range_description = "Recent firm activity through now"
        anchored_on = "activity"
    time_range = {
        "start_ts": range_start_ts,
        "end_ts": range_end_ts,
        "start_label": _fmt_range(range_start_ts),
        "end_label": _fmt_range(range_end_ts),
        "first_trade_ts": first_trade_ts,
        "anchored_on": anchored_on,
        "description": range_description,
    }
    sector_mix = []
    try:
        fs = firm_state.build_firm_state(refresh_prices=False)
        for s in fs.get("sectors") or []:
            if float(s.get("pct_nav") or 0) > 0.001:
                sector_mix.append({
                    "sector": s["sector"],
                    "pct": round(float(s["pct_nav"]) * 100, 1),
                })
    except Exception:
        pass

    policy = {
        "cash_target_pct": round(allocation.CASH_TARGET_PCT * 100, 1),
        "cash_floor_pct": round(allocation.CASH_FLOOR_PCT * 100, 1),
        "min_invested_pct": round(allocation.MIN_INVESTED_PCT * 100, 1),
        "target_invested_pct": round(allocation.TARGET_INVESTED_PCT * 100, 1),
        "max_invested_pct": round(allocation.MAX_INVESTED_PCT * 100, 1),
    }

    return {
        "as_of": time.strftime("%Y-%m-%d %H:%M"),
        "has_data": len(metrics) >= 2 or len(events) >= 3,
        "time_range": time_range,
        "policy": policy,
        "metrics": metrics,
        "events": events[-120:],
        "sector_mix": sector_mix[:12],
        "event_counts": {
            "run": sum(1 for e in events if e["kind"] == "run"),
            "trade": sum(1 for e in events if e["kind"] == "trade"),
            "hitl": sum(1 for e in events if e["kind"] == "hitl"),
        },
    }
