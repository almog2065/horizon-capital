"""Keep hitl_queue, pending_hitl plans, and awaiting_hitl runs in sync."""
from __future__ import annotations

import json
from typing import Any, Optional

from . import db


def _best_run_for_plan(plan_id: str) -> Optional[str]:
    """Prefer an awaiting_hitl run for this plan (only while plan is pending_hitl)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT run_id, status, state_json, created_at FROM runs "
            "WHERE status='awaiting_hitl' ORDER BY created_at DESC",
        ).fetchall()
    for row in rows:
        row = dict(row)
        try:
            state = json.loads(row.get("state_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if db.plan_id_from_run_state(state) == plan_id:
            return row["run_id"]
    return None


def reconcile_stale_pending_plans() -> int:
    """Fix plans stuck in pending_hitl after HITL was already resolved."""
    n = 0
    for row in db.list_plans(status="pending_hitl"):
        pid = row["plan_id"]
        hitl = db.hitl_for_plan(pid)
        if not hitl or hitl.get("status") != "resolved":
            continue
        resolution = hitl.get("resolution") or ""
        run = db.get_run(hitl.get("run_id") or "")
        final = ""
        if run:
            try:
                final = json.loads(run.get("state_json") or "{}").get("final_status") or ""
            except (json.JSONDecodeError, TypeError):
                pass
        if resolution == "reject":
            db.update_plan_status(
                pid, "rejected",
                rejection_reason="HITL rejected (reconciled)",
            )
            n += 1
        elif resolution == "approve":
            if final == "completed_position_opened":
                db.update_plan_status(pid, "active", approved_by="operator")
                n += 1
            elif final in (
                "completed_supervisor_blocked",
                "completed_hitl_rejected",
            ):
                db.update_plan_status(
                    pid, "draft",
                    history_append={
                        "agent": "system",
                        "action": "reconciled",
                        "note": f"Unblocked after {final}",
                    },
                )
                n += 1
    return n


def _prune_invalid_queue_rows() -> int:
    """Drop queue rows whose plan is no longer pending_hitl."""
    n = 0
    with db.conn() as c:
        for item in db.list_hitl_pending():
            plan_row = db.get_plan(item["plan_id"])
            if plan_row and plan_row.get("status") == "pending_hitl":
                continue
            c.execute(
                "DELETE FROM hitl_queue WHERE item_id=? AND status='pending'",
                (item["item_id"],),
            )
            n += 1
        c.commit()
    return n


def repair_hitl_queue() -> dict[str, Any]:
    """Ensure every pending_hitl plan has a pending hitl_queue row."""
    stats: dict[str, Any] = {
        "reconciled_plans": 0,
        "enqueued": 0,
        "run_ids_updated": 0,
        "pruned": 0,
        "runs_closed": 0,
    }
    stats["reconciled_plans"] = reconcile_stale_pending_plans()
    stats["pruned"] = _prune_invalid_queue_rows()

    pending_ids = {i["plan_id"] for i in db.list_hitl_pending()}

    for row in db.list_plans(status="pending_hitl"):
        pid = row["plan_id"]
        hitl = db.hitl_for_plan(pid)
        if hitl and hitl.get("status") == "pending":
            pending_ids.add(pid)
            run_id = _best_run_for_plan(pid)
            if run_id and hitl.get("run_id") != run_id:
                _update_hitl_run(hitl["item_id"], run_id)
                stats["run_ids_updated"] += 1
            continue
        run_id = _best_run_for_plan(pid) or f"repair_{pid[:12]}"
        item_id = db.enqueue_hitl(run_id, pid)
        if item_id:
            stats["enqueued"] += 1
            pending_ids.add(pid)

    with db.conn() as c:
        run_rows = c.execute(
            "SELECT run_id, state_json FROM runs WHERE status='awaiting_hitl'",
        ).fetchall()

    for row in run_rows:
        row = dict(row)
        try:
            state = json.loads(row.get("state_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        pid = db.plan_id_from_run_state(state)
        if not pid or pid in pending_ids:
            continue
        plan_row = db.get_plan(pid)
        if not plan_row or plan_row.get("status") != "pending_hitl":
            continue
        item_id = db.enqueue_hitl(row["run_id"], pid)
        if item_id:
            stats["enqueued"] += 1
            pending_ids.add(pid)

    close = db.close_stale_hitl_runs()
    stats["runs_closed"] = close.get("closed", 0)
    stats["pending_queue"] = len(db.list_hitl_pending())
    return stats


def _update_hitl_run(item_id: int, run_id: str) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE hitl_queue SET run_id=? WHERE item_id=? AND status='pending'",
            (run_id, item_id),
        )
        c.commit()


def list_actionable_hitl() -> list[dict]:
    """Pending queue rows after repair (dashboard + manager use this)."""
    repair_hitl_queue()
    return db.list_hitl_pending()
