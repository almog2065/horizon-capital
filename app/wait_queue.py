"""Build unified 'what is waiting' view for the dashboard."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from . import config, db


def _age_sec(since: float, now: float) -> float:
    return max(0.0, now - float(since or now))


def _supervision_schedule(now: float) -> dict[str, Any]:
    interval = max(60, int(config.PLAN_SUPERVISION_INTERVAL_SEC))
    enabled = config.AUTO_PLAN_SUPERVISION
    last_at: Optional[float] = None
    last_id: Optional[str] = None
    with db.conn() as c:
        row = c.execute(
            "SELECT run_id, created_at FROM runs "
            "WHERE trigger_type='plan_supervision' "
            "ORDER BY created_at DESC LIMIT 1",
        ).fetchone()
        if row:
            last_at = float(row["created_at"])
            last_id = row["run_id"]
    next_at = (last_at + interval) if last_at else None
    if enabled and next_at and next_at < now:
        next_at = now  # overdue — due now
    return {
        "enabled": enabled,
        "interval_sec": interval,
        "interval_min": interval // 60,
        "auto_execute": config.AUTO_PLAN_EXECUTE,
        "spawn_pipeline": config.AUTO_PLAN_SPAWN_PIPELINE,
        "last_run_at": last_at,
        "last_run_id": last_id,
        "next_run_at": next_at if enabled else None,
        "seconds_until_next": max(0.0, (next_at - now)) if next_at else None,
        "overdue": bool(enabled and next_at and next_at <= now + 1),
    }


def build_wait_queue(now: Optional[float] = None) -> dict[str, Any]:
    """All operations blocked on operator, HITL, or the next automation tick."""
    now = now or time.time()
    items: list[dict[str, Any]] = []
    pending_plan_ids = {i["plan_id"] for i in db.list_hitl_pending()}

    for row in db.list_hitl_pending():
        plan = db.get_plan(row["plan_id"]) or {}
        ticker = (plan.get("ticker") or "?").upper()
        since = float(row.get("created_at") or now)
        items.append({
            "kind": "hitl_operator",
            "kind_label": "Operator approval",
            "icon": "⏸",
            "ticker": ticker,
            "title": f"{ticker} — HITL sign-off",
            "detail": f"Plan {row['plan_id'][:16]}… · run {row['run_id']}",
            "waiting_since": since,
            "wait_seconds": _age_sec(since, now),
            "href": f"/hitl/{row['item_id']}",
            "ref_id": str(row["item_id"]),
        })

    with db.conn() as c:
        run_rows = c.execute(
            "SELECT run_id, trigger_type, status, as_of, created_at, state_json "
            "FROM runs WHERE status='awaiting_hitl' ORDER BY created_at ASC",
        ).fetchall()

    for row in run_rows:
        row = dict(row)
        since = float(row.get("created_at") or now)
        ticker = "?"
        plan_id = ""
        try:
            st = json.loads(row.get("state_json") or "{}")
            ticker = (st.get("ticker") or "?").upper()
            plan_id = db.plan_id_from_run_state(st) or ""
        except (json.JSONDecodeError, TypeError):
            st = {}
        if plan_id and plan_id in pending_plan_ids:
            continue  # already shown as HITL card
        items.append({
            "kind": "run_awaiting_hitl",
            "kind_label": "Run paused (HITL)",
            "icon": "⏳",
            "ticker": ticker,
            "title": f"Run {row['run_id']}",
            "detail": row.get("trigger_type", "news_event"),
            "waiting_since": since,
            "wait_seconds": _age_sec(since, now),
            "href": f"/run/{row['run_id']}",
            "ref_id": row["run_id"],
        })

    for row in db.list_plans(status="pending_hitl"):
        pid = row["plan_id"]
        if pid in pending_plan_ids:
            continue
        since = float(row.get("created_at") or now)
        ok, _ = db.can_delete_plan(pid)
        items.append({
            "kind": "plan_pending_hitl",
            "kind_label": "Plan pending (no queue)",
            "icon": "⚠",
            "ticker": (row.get("ticker") or "?").upper(),
            "title": f"{row.get('ticker', '?')} plan stuck",
            "detail": pid,
            "waiting_since": since,
            "wait_seconds": _age_sec(since, now),
            "href": f"/plan/{pid}",
            "ref_id": pid,
            "can_delete": ok,
        })

    for row in db.list_plans(status="draft"):
        since = float(row.get("created_at") or now)
        pid = row["plan_id"]
        ok, _ = db.can_delete_plan(pid)
        items.append({
            "kind": "plan_draft",
            "kind_label": "Draft plan",
            "icon": "📝",
            "ticker": (row.get("ticker") or "?").upper(),
            "title": f"{row.get('ticker', '?')} draft",
            "detail": "Awaiting supervision / fill path",
            "waiting_since": since,
            "wait_seconds": _age_sec(since, now),
            "href": f"/plan/{pid}",
            "ref_id": pid,
            "can_delete": ok,
        })

    with db.conn() as c:
        running = c.execute(
            "SELECT run_id, trigger_type, created_at FROM runs "
            "WHERE status='running' ORDER BY created_at ASC",
        ).fetchall()

    for row in running:
        row = dict(row)
        since = float(row.get("created_at") or now)
        age = _age_sec(since, now)
        if age < 120:
            continue
        items.append({
            "kind": "run_running",
            "kind_label": "Long-running job",
            "icon": "🔄",
            "ticker": "",
            "title": row["run_id"],
            "detail": row.get("trigger_type", ""),
            "waiting_since": since,
            "wait_seconds": age,
            "href": f"/run/{row['run_id']}",
            "ref_id": row["run_id"],
        })

    items.sort(key=lambda x: (-x["wait_seconds"], x.get("ticker", "")))

    by_kind: dict[str, int] = {}
    for it in items:
        by_kind[it["kind"]] = by_kind.get(it["kind"], 0) + 1

    supervision = _supervision_schedule(now)

    return {
        "now": now,
        "entries": items,
        "supervision": supervision,
        "summary": {
            "total": len(items),
            "operator_hitl": by_kind.get("hitl_operator", 0),
            "runs_paused": by_kind.get("run_awaiting_hitl", 0),
            "plans_stuck": by_kind.get("plan_pending_hitl", 0),
            "drafts": by_kind.get("plan_draft", 0),
            "long_runs": by_kind.get("run_running", 0),
        },
    }
