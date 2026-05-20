"""Portfolio analytics for the dashboard — NAV, cash, yield, expectations."""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import config, db, tools


def _horizon_days(horizon_str: str) -> int:
    """Parse plan horizons like '12-18 months' → midpoint days."""
    if not horizon_str:
        return 365
    months = [int(x) for x in re.findall(r"\d+", horizon_str)]
    if len(months) >= 2:
        avg_months = (months[0] + months[1]) / 2
    elif months:
        avg_months = months[0]
    else:
        avg_months = 12
    if "month" in horizon_str.lower():
        return int(avg_months * 30.44)
    if "year" in horizon_str.lower():
        return int(avg_months * 365)
    return int(avg_months * 30)


def _annualized_return(return_pct: float, days: int) -> float:
    if days <= 0:
        return 0.0
    return (1 + return_pct) ** (365.0 / days) - 1


def _load_closed_plans() -> list[dict]:
    """Historical closed plans from data/past_plans (firm track record)."""
    closed: list[dict] = []
    plans_dir = config.PAST_PLANS_DIR
    if not plans_dir.exists():
        return closed
    for p in sorted(plans_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        outcome = data.get("outcome") or {}
        days = int(outcome.get("holding_period_days") or 0)
        ret = float(outcome.get("realized_return_pct") or 0)
        closed.append({
            "plan_id": data.get("plan_id", p.stem),
            "ticker": data.get("ticker", "?"),
            "sector": data.get("sector", ""),
            "realized_return_pct": ret,
            "holding_period_days": days,
            "annualized_return_pct": _annualized_return(ret, days) if days else 0,
            "thesis_validated": outcome.get("thesis_validated", ""),
            "exit_date": data.get("exit_date", ""),
        })
    return closed


def safe_float(val: Any, default: float = 0.0) -> float:
    """Parse numeric plan/valuation fields; ignore N/A and other non-numeric tokens."""
    if val is None:
        return default
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s or s.upper() in ("N/A", "NA", "NONE", "—", "-", "UNKNOWN"):
            return default
        try:
            return float(s)
        except ValueError:
            m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", s)
            return float(m.group()) if m else default
    return default


def _plan_for_holding(plan_id: Optional[str], ticker: Optional[str] = None) -> Optional[dict]:
    if plan_id:
        row = db.get_plan(plan_id)
        if row and row.get("status") == "active":
            return json.loads(row["plan_json"])
    if ticker:
        canon = db.canonical_active_plan_for_ticker(ticker)
        if canon:
            row = db.get_plan(canon["plan_id"])
            if row:
                return json.loads(row["plan_json"])
    if plan_id:
        row = db.get_plan(plan_id)
        if row:
            return json.loads(row["plan_json"])
    return None


def _expected_upside_pct(plan: dict, current_price: float) -> float:
    """Rough upside to valuation band high vs current price."""
    thesis = plan.get("thesis") or {}
    band = thesis.get("valuation_target_range") or {}
    low = safe_float(band.get("low"))
    high = safe_float(band.get("high"))
    if high <= 0 or current_price <= 0:
        return 0.12  # demo default
    metric = (band.get("metric") or "").lower()
    if metric in ("p_e", "pe", "ev_ebitda"):
        # Band is multiple space — map to ~15% upside if within band
        mid = (low + high) / 2 if low else high
        if mid <= 0:
            return 0.10
        return max(0.0, min(0.50, (high - mid) / mid))
    # Price-like band
    return max(0.0, min(0.50, (high - current_price) / current_price))


def _realized_pnl_from_ledger(nav: float) -> tuple[float, int]:
    """Sum closed-trade P&L from the firm trade ledger (archive + live sells)."""
    from . import trade_history

    if db.trade_history_count() == 0:
        trade_history.ensure_trade_history_seeded()
    realized = 0.0
    closes = 0
    for t in db.list_trade_history(limit=500):
        if t.get("action") not in ("close", "sell"):
            continue
        meta = t.get("meta") or {}
        ret = float(meta.get("realized_return_pct") or 0)
        notional = float(t.get("notional_usd") or 0)
        if notional <= 0:
            notional = nav * 0.04
        realized += notional * ret
        closes += 1
    return realized, closes


def get_portfolio_summary(refresh_prices: bool = True) -> dict[str, Any]:
    nav = float(config.STARTING_NAV)
    holdings_raw = db.list_holdings()

    positions: list[dict] = []
    invested = 0.0
    total_cost = 0.0

    for h in holdings_raw:
        ticker = h["ticker"]
        qty = int(h["quantity"])
        cost = float(h["cost_basis"])
        if refresh_prices:
            quote = tools.fetch_quote(ticker)
            price = float(quote["price"])
            db.upsert_holding(
                ticker, qty, cost, price, h.get("plan_id", ""), h.get("sector", ""),
            )
        else:
            price = float(h["current_price"])

        market_value = qty * price
        cost_total = qty * cost
        pnl = market_value - cost_total
        pnl_pct = (pnl / cost_total) if cost_total else 0.0

        plan = _plan_for_holding(h.get("plan_id"), ticker=ticker)
        horizon_days = 365
        days_held = 0
        expected_return_pct = 0.10
        if plan:
            horizon_str = (plan.get("thesis") or {}).get("expected_holding_horizon", "")
            horizon_days = _horizon_days(horizon_str)
            created = plan.get("created_at") or plan.get("approved_at") or ""
            if created:
                try:
                    created_ts = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
                    days_held = max(0, int((time.time() - created_ts) / 86400))
                except Exception:
                    days_held = 0
            expected_return_pct = _expected_upside_pct(plan, price)

        days_remaining = max(0, horizon_days - days_held)
        expected_pnl = market_value * expected_return_pct

        invested += market_value
        total_cost += cost_total

        positions.append({
            "ticker": ticker,
            "quantity": qty,
            "price": price,
            "cost_basis": cost,
            "market_value": market_value,
            "cost_total": cost_total,
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pnl_pct,
            "pct_nav": market_value / nav if nav else 0,
            "sector": h.get("sector") or "Unknown",
            "plan_id": h.get("plan_id"),
            "horizon_days": horizon_days,
            "days_held": days_held,
            "days_remaining": days_remaining,
            "expected_return_pct": expected_return_pct,
            "expected_pnl_usd": expected_pnl,
        })

    cash = nav - invested
    cash = max(0.0, cash)
    unrealized_pnl = invested - total_cost
    unrealized_pnl_pct = (unrealized_pnl / total_cost) if total_cost else 0.0
    portfolio_return_pct = unrealized_pnl / nav if nav else 0.0

    # Pending HITL deployment
    pending_deploy = 0.0
    pending_plans: list[dict] = []
    for p_row in db.list_plans(status="pending_hitl"):
        plan = db.load_plan_body(p_row["plan_id"])
        if not plan:
            continue
        entry = plan.get("entry") or {}
        pct = float(entry.get("target_size_pct_nav") or 0)
        pending_deploy += nav * pct
        pending_plans.append({
            "plan_id": p_row["plan_id"],
            "ticker": p_row["ticker"],
            "notional_usd": nav * pct,
            "target_pct_nav": pct,
        })

    # Expected portfolio (active positions)
    expected_pnl_total = sum(p["expected_pnl_usd"] for p in positions)
    if positions:
        avg_days_remaining = int(
            sum(p["days_remaining"] for p in positions) / len(positions)
        )
        weighted_expected_pct = (
            sum(p["expected_return_pct"] * p["market_value"] for p in positions)
            / invested if invested else 0
        )
    else:
        avg_days_remaining = 0
        weighted_expected_pct = 0.0

    closed = _load_closed_plans()
    if closed:
        past_avg_return = sum(c["realized_return_pct"] for c in closed) / len(closed)
        past_avg_annualized = sum(c["annualized_return_pct"] for c in closed) / len(closed)
        past_avg_days = int(sum(c["holding_period_days"] for c in closed) / len(closed))
    else:
        past_avg_return = past_avg_annualized = 0.0
        past_avg_days = 0

    sector_allocation: dict[str, float] = {}
    for p in positions:
        s = p["sector"]
        sector_allocation[s] = sector_allocation.get(s, 0.0) + p["market_value"]

    sectors = [
        {
            "sector": s,
            "usd": v,
            "pct_nav": v / nav if nav else 0,
        }
        for s, v in sorted(sector_allocation.items(), key=lambda x: -x[1])
    ]

    from . import allocation
    cash_floor_pct = allocation.CASH_FLOOR_PCT
    liq = allocation.liquidity_budget(
        nav, cash, pending_deploy_usd=pending_deploy, maiden_entry=True,
    )
    deployable_cash = liq["deployable_cash_usd"]

    realized_pnl_usd, realized_close_count = _realized_pnl_from_ledger(nav)
    firm_profit_usd = unrealized_pnl + realized_pnl_usd
    firm_profit_pct = firm_profit_usd / nav if nav else 0.0
    economic_nav_usd = nav + firm_profit_usd

    return {
        "starting_nav_usd": nav,
        "nav_usd": nav,
        "economic_nav_usd": economic_nav_usd,
        "firm_profit_usd": firm_profit_usd,
        "firm_profit_pct": firm_profit_pct,
        "realized_pnl_usd": realized_pnl_usd,
        "realized_pnl_pct": realized_pnl_usd / nav if nav else 0.0,
        "realized_close_count": realized_close_count,
        "invested_usd": invested,
        "cash_usd": cash,
        "invested_pct": invested / nav if nav else 0,
        "cash_pct": cash / nav if nav else 0,
        "deployable_cash_usd": deployable_cash,
        "liquidity": liq,
        "cash_floor_pct": cash_floor_pct,
        "total_cost_usd": total_cost,
        "unrealized_pnl_usd": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "portfolio_return_pct": portfolio_return_pct,
        "positions_count": len(positions),
        "positions": sorted(positions, key=lambda p: -p["market_value"]),
        "pending_deploy_usd": pending_deploy,
        "pending_plans": pending_plans,
        "expected_pnl_usd": expected_pnl_total,
        "expected_return_pct_nav": (
            expected_pnl_total / nav if nav else 0
        ),
        "weighted_expected_return_pct": weighted_expected_pct,
        "avg_days_remaining": avg_days_remaining,
        "closed_plans": closed,
        "past_avg_return_pct": past_avg_return,
        "past_avg_annualized_pct": past_avg_annualized,
        "past_avg_holding_days": past_avg_days,
        "sectors": sectors,
        "as_of": time.strftime("%Y-%m-%d %H:%M"),
    }


def fmt_usd(amount: float) -> str:
    a = float(amount)
    if abs(a) >= 1_000_000:
        return f"${a / 1_000_000:,.2f}M"
    if abs(a) >= 10_000:
        return f"${a:,.0f}"
    return f"${a:,.2f}"


def fmt_pct(ratio: float, signed: bool = True) -> str:
    pct = float(ratio) * 100
    if signed:
        return f"{pct:+.2f}%"
    return f"{pct:.2f}%"
