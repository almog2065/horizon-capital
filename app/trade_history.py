"""Firm trading history — live fills, archived closes, HITL outcomes."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import config, db, portfolio


def record_trade(
    *,
    ticker: str,
    action: str,
    side: str = "long",
    quantity: int = 0,
    price: float = 0.0,
    notional_usd: Optional[float] = None,
    plan_id: str = "",
    run_id: str = "",
    sector: str = "",
    source: str = "live",
    as_of: Optional[str] = None,
    ts: Optional[float] = None,
    meta: Optional[dict] = None,
    trade_id: Optional[str] = None,
) -> dict:
    """Persist one trade ledger row."""
    ts = ts if ts is not None else time.time()
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    notional = notional_usd if notional_usd is not None else price * quantity
    tid = trade_id or f"{action}_{ticker}_{int(ts)}_{run_id[:8] if run_id else 'x'}"
    row = {
        "trade_id": tid,
        "ts": ts,
        "as_of": as_of,
        "ticker": ticker.upper(),
        "side": side,
        "action": action,
        "quantity": quantity,
        "price": price,
        "notional_usd": notional,
        "plan_id": plan_id,
        "run_id": run_id,
        "sector": sector,
        "source": source,
        "meta": meta or {},
    }
    db.insert_trade(row)
    return row


def record_from_fill(
    fill: dict,
    *,
    plan_id: str,
    run_id: str,
    as_of: str,
    sector: str = "",
    action: str = "buy",
) -> Optional[dict]:
    if fill.get("status") != "filled":
        return None
    return record_trade(
        ticker=fill.get("ticker", "?"),
        action=action,
        side=fill.get("side", "long"),
        quantity=int(fill.get("quantity") or 0),
        price=float(fill.get("fill_price") or 0),
        notional_usd=float(fill.get("notional_usd") or 0),
        plan_id=plan_id,
        run_id=run_id,
        sector=sector,
        source="live",
        as_of=as_of,
        trade_id=f"fill_{run_id}_{fill.get('ticker', '').lower()}",
        meta={"fill_status": fill.get("status")},
    )


def _import_archived_closes() -> int:
    """Seed closed trades from data/past_plans/*.json."""
    n = 0
    plans_dir = config.PAST_PLANS_DIR
    if not plans_dir.exists():
        return 0
    for p in sorted(plans_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        plan_id = data.get("plan_id", p.stem)
        ticker = (data.get("ticker") or "?").upper()
        outcome = data.get("outcome") or {}
        exit_date = data.get("exit_date") or ""
        entry_date = data.get("entry_date") or ""
        try:
            ts = time.mktime(time.strptime(exit_date[:10], "%Y-%m-%d"))
        except (ValueError, TypeError):
            ts = time.time() - 86400 * 365
        ret = float(outcome.get("realized_return_pct") or 0)
        days = int(outcome.get("holding_period_days") or 0)
        notional = config.STARTING_NAV * 0.04
        record_trade(
            ticker=ticker,
            action="sell",
            side="long",
            quantity=0,
            price=0.0,
            notional_usd=notional,
            plan_id=plan_id,
            run_id="",
            sector=data.get("sector", ""),
            source="archive",
            as_of=exit_date or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
            ts=ts,
            trade_id=f"archive_close_{plan_id}",
            meta={
                "realized_return_pct": ret,
                "holding_period_days": days,
                "thesis_validated": outcome.get("thesis_validated"),
                "entry_date": entry_date,
                "thesis_summary": (data.get("thesis_summary") or "")[:300],
                "lesson_learned": (data.get("lesson_learned") or "")[:200],
            },
        )
        n += 1
    return n


def _backfill_from_runs() -> int:
    """Import filled orders and HITL outcomes from persisted runs."""
    n = 0
    with db.conn() as c:
        rows = c.execute(
            "SELECT run_id, as_of, created_at, state_json FROM runs "
            "ORDER BY created_at ASC",
        ).fetchall()
    for row in rows:
        try:
            state = json.loads(row["state_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        run_id = row["run_id"]
        as_of = row["as_of"] or ""
        ts = float(row["created_at"] or time.time())
        fill = state.get("fill") or {}
        if fill.get("status") == "filled":
            plan_id = db.plan_id_from_run_state(state) or ""
            sector = ""
            if plan_id:
                pr = db.get_plan(plan_id)
                if pr:
                    try:
                        body = json.loads(pr["plan_json"])
                        sector = body.get("sector", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
            record_from_fill(
                fill, plan_id=plan_id, run_id=run_id, as_of=as_of,
                sector=sector, action="buy",
            )
            n += 1
        final = state.get("final_status") or ""
        if final == "completed_hitl_rejected":
            ticker = (state.get("ticker") or "?").upper()
            plan_id = db.plan_id_from_run_state(state) or ""
            record_trade(
                ticker=ticker,
                action="rejected",
                plan_id=plan_id,
                run_id=run_id,
                source="live",
                as_of=as_of,
                ts=ts,
                trade_id=f"reject_{run_id}",
                meta={"final_status": final},
            )
            n += 1
    return n


def ensure_trade_history_seeded() -> dict[str, int]:
    """Idempotent backfill of archive + runs into trade_history."""
    before = db.trade_history_count()
    archived = _import_archived_closes()
    from_runs = _backfill_from_runs()
    after = db.trade_history_count()
    return {
        "before": before,
        "after": after,
        "archived_imported": archived,
        "runs_imported": from_runs,
    }


def buy_sell_label(action: str, side: str = "long") -> str:
    """Map ledger action + position side to operator-facing Buy / Sell."""
    act = (action or "").lower()
    sd = (side or "long").lower()
    if act in ("open", "buy"):
        return "Sell" if sd == "short" else "Buy"
    if act in ("close", "sell"):
        return "Buy" if sd == "short" else "Sell"
    if act == "rejected":
        return "Rejected"
    if act == "cancelled":
        return "Cancelled"
    if act in ("buy", "sell"):
        return act.capitalize()
    return action.title() if action else "—"


def _summary(trades: list[dict]) -> dict[str, Any]:
    buys = [t for t in trades if buy_sell_label(t["action"], t.get("side")) == "Buy"]
    sells = [t for t in trades if buy_sell_label(t["action"], t.get("side")) == "Sell"]
    rejected = [t for t in trades if t["action"] == "rejected"]
    live_buys = [
        t for t in buys
        if t.get("source") == "live" and t["action"] in ("open", "buy")
    ]
    total_buy_notional = sum(float(t.get("notional_usd") or 0) for t in live_buys)
    sell_closes = [t for t in trades if t["action"] in ("close", "sell")]
    wins = [
        t for t in sell_closes
        if float((t.get("meta") or {}).get("realized_return_pct") or 0) > 0
    ]
    avg_close_ret = 0.0
    if sell_closes:
        avg_close_ret = sum(
            float((t.get("meta") or {}).get("realized_return_pct") or 0)
            for t in sell_closes
        ) / len(sell_closes)
    return {
        "total": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "opens": len([t for t in trades if t["action"] in ("open", "buy")]),
        "closes": len(sell_closes),
        "rejected": len(rejected),
        "live_fills": len(live_buys),
        "total_notional_open_usd": total_buy_notional,
        "win_rate_closed": (len(wins) / len(sell_closes)) if sell_closes else None,
        "avg_close_return_pct": avg_close_ret,
    }


def fmt_trade_ts(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (ValueError, TypeError, OSError):
        return "—"


def get_firm_trade_history(limit: int = 200) -> dict[str, Any]:
    if db.trade_history_count() == 0:
        ensure_trade_history_seeded()
    trades = db.list_trade_history(limit=limit)
    for t in trades:
        label = buy_sell_label(t["action"], t.get("side"))
        t["buy_sell"] = label.lower() if label in ("Buy", "Sell") else ""
        t["action_label"] = label
        if t["action"] in ("close", "sell") and t.get("meta"):
            t["return_pct"] = float(t["meta"].get("realized_return_pct") or 0)
            t["holding_days"] = t["meta"].get("holding_period_days")
        elif t["action"] in ("open", "buy"):
            t["return_pct"] = None
            t["holding_days"] = None
        else:
            t.setdefault("return_pct", None)
            t.setdefault("holding_days", None)
    return {
        "trades": trades,
        "summary": _summary(trades),
        "as_of": time.strftime("%Y-%m-%d %H:%M"),
    }
