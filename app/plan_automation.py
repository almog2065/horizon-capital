"""Automated plan supervision — all plans, with or without open positions."""
from __future__ import annotations

from typing import Optional

from . import allocation, config, db, tools
from .agents import position_monitor
from .agents.plan_supervisor import MONITORED_STATUSES

_DEFAULT_ENTRY_PCT = 0.04


def order_quantity_from_plan(
    plan: dict,
    *,
    min_qty: int = 1,
    nav_usd: float | None = None,
    cap_by_liquidity: bool = True,
) -> int:
    """Share count from plan entry (% NAV) and limit/quote price."""
    entry = plan.get("entry") or {}
    pct = float(entry.get("target_size_pct_nav") or 0)
    if pct <= 0:
        pct = _DEFAULT_ENTRY_PCT

    trigger = entry.get("entry_price_or_trigger") or {}
    price = float(trigger.get("value") or 0)
    ticker = (plan.get("ticker") or "").upper()
    if price <= 0 and ticker:
        quote = tools.fetch_quote(ticker)
        price = float(quote.get("price") or 0)
    if price <= 0:
        return 0

    nav = float(nav_usd if nav_usd is not None else config.STARTING_NAV)
    maiden = True
    try:
        for h in db.list_holdings():
            if (h.get("ticker") or "").upper() == ticker and int(h.get("quantity") or 0) > 0:
                maiden = False
                break
    except Exception:
        pass
    notional = pct * nav
    if cap_by_liquidity:
        liq = _liquidity_for_nav(nav, maiden_entry=maiden)
        pct = allocation.cap_entry_pct_for_liquidity(pct, liq, maiden=maiden)
        notional = min(pct * nav, liq["deployable_cash_usd"])
    qty = int(round(notional / price)) if notional > 0 else 0
    if min_qty > 0 and qty < min_qty and notional > 0:
        qty = min_qty
    return qty


def _liquidity_for_nav(nav: float, *, maiden_entry: bool = True) -> dict:
    try:
        holdings = db.list_holdings()
    except Exception:
        holdings = []
    invested = sum(
        int(h.get("quantity") or 0) * float(h.get("current_price") or 0)
        for h in holdings
    )
    cash = max(0.0, nav - invested)
    pending = 0.0
    for row in db.list_plans(status="pending_hitl"):
        body = db.load_plan_body(row["plan_id"])
        if body:
            pending += nav * float((body.get("entry") or {}).get("target_size_pct_nav") or 0)
    return allocation.liquidity_budget(
        nav, cash, pending_deploy_usd=pending, maiden_entry=maiden_entry,
    )


def holding_for_plan(plan: dict) -> Optional[dict]:
    pid = plan.get("id")
    ticker = plan.get("ticker", "").upper()
    for h in db.list_holdings():
        if h.get("plan_id") == pid or h.get("ticker") == ticker:
            if int(h.get("quantity") or 0) > 0:
                return h
    return None


def synthetic_holding(plan: dict) -> dict:
    """Placeholder holding for monitor pre-fill / active-without-position."""
    ticker = plan["ticker"]
    entry = float(
        (plan.get("entry") or {}).get("entry_price_or_trigger", {}).get("value") or 0
    )
    quote = tools.fetch_quote(ticker)
    price = float(quote.get("price") or entry or 1)
    qty = order_quantity_from_plan(plan, min_qty=0)
    dossier = tools.get_dossier(ticker)
    sector = "Unknown"
    if dossier.get("found"):
        sector = dossier["dossier"].get("sector", "Unknown")
    return {
        "ticker": ticker,
        "quantity": qty,
        "cost_basis": entry or price,
        "current_price": price,
        "plan_id": plan.get("id"),
        "sector": sector,
        "_synthetic": True,
    }


def list_supervisable_plans() -> list[dict]:
    rows = []
    active_tickers_seen: set[str] = set()
    for row in db.list_plans():
        if row["status"] not in MONITORED_STATUSES:
            continue
        if row["status"] == "active":
            t = (row.get("ticker") or "").upper()
            canon = db.canonical_active_plan_for_ticker(t)
            if canon and canon["plan_id"] != row["plan_id"]:
                continue
            if t in active_tickers_seen:
                continue
            active_tickers_seen.add(t)
        plan = db.load_plan_body(row["plan_id"])
        if not plan:
            print(f"[plan_automation] skip {row['plan_id']}: missing or invalid plan_json")
            continue
        plan["status"] = row["status"]
        rows.append({"row": row, "plan": plan})
    return rows


def evaluate_plan(
    plan: dict,
    plan_status: str,
    as_of: str,
    holdings_tickers: list[str],
    watchlist_tickers: list[str],
    firm_state: Optional[dict] = None,
    manager_out: Optional[dict] = None,
) -> dict:
    """Run monitor (if positioned) + build evaluation bundle for supervisor."""
    holding = holding_for_plan(plan)
    has_position = holding is not None and int(holding.get("quantity") or 0) > 0
    phase = "in_position" if has_position else "pre_position"
    h = holding if has_position else synthetic_holding(plan)

    monitor_report = None
    if plan_status == "active" or has_position:
        monitor_report = position_monitor.run(
            plan["ticker"], h, plan, as_of=as_of,
            holdings_tickers=holdings_tickers,
            watchlist_tickers=watchlist_tickers,
            firm_state=firm_state,
            manager_out=manager_out,
        )

    return {
        "ticker": plan["ticker"],
        "plan_id": plan["id"],
        "plan_status": plan_status,
        "phase": phase,
        "has_position": has_position,
        "holding": holding,
        "monitor_report": monitor_report,
        "supervisor_report": None,
    }


def append_supervision_history(plan_id: str, evaluation: dict, cycle_run_id: str):
    sup = evaluation.get("supervisor_report") or {}
    db.append_plan_history(plan_id, {
        "agent": "plan_supervisor",
        "action": "supervision_cycle",
        "run_id": cycle_run_id,
        "verdict": sup.get("verdict"),
        "note": (sup.get("reasoning_narrative") or "")[:400],
    })
