"""SQLite persistence: holdings, plans, runs, journal."""
from __future__ import annotations
import sqlite3
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Any
from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    trigger_type TEXT,
    trigger_meta TEXT,
    as_of TEXT,
    status TEXT,
    state_json TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    ticker TEXT,
    status TEXT,
    plan_json TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS holdings (
    ticker TEXT PRIMARY KEY,
    quantity INTEGER,
    cost_basis REAL,
    current_price REAL,
    plan_id TEXT,
    sector TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    agent TEXT,
    output_json TEXT,
    duration_ms INTEGER,
    ts REAL
);
CREATE TABLE IF NOT EXISTS audit_notes (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    about_journal_id INTEGER,
    severity TEXT,
    compliant INTEGER,
    note_json TEXT,
    ts REAL
);
CREATE TABLE IF NOT EXISTS hitl_queue (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    plan_id TEXT,
    status TEXT,
    created_at REAL,
    resolved_at REAL,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS idea_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    scan_run_id TEXT,
    composite_score REAL,
    recommended_action TEXT,
    brief_json TEXT,
    suggested_at REAL
);
CREATE INDEX IF NOT EXISTS idx_idea_history_ticker ON idea_history(ticker);
CREATE INDEX IF NOT EXISTS idx_idea_history_run ON idea_history(scan_run_id);
CREATE TABLE IF NOT EXISTS trade_history (
    trade_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    as_of TEXT,
    ticker TEXT NOT NULL,
    side TEXT,
    action TEXT NOT NULL,
    quantity INTEGER,
    price REAL,
    notional_usd REAL,
    plan_id TEXT,
    run_id TEXT,
    sector TEXT,
    source TEXT,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_history_ts ON trade_history(ts DESC);
CREATE INDEX IF NOT EXISTS idx_trade_history_ticker ON trade_history(ticker);
"""


def init_db():
    config.FIRM_DB.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        c.commit()


@contextmanager
def conn():
    c = sqlite3.connect(config.FIRM_DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def save_run(run_id: str, trigger_type: str, trigger_meta: dict, as_of: str,
             status: str, state: dict):
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, trigger_type, json.dumps(trigger_meta), as_of, status,
             json.dumps(state), now, now),
        )
        c.commit()


def patch_run_state(run_id: str, patch: dict, *, status: Optional[str] = None) -> bool:
    """Merge ``patch`` into run state_json (for in-flight progress checkpoints)."""
    row = get_run(run_id)
    if not row:
        return False
    try:
        state = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        state = {}
    state.update(patch)
    meta = row.get("trigger_meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    save_run(
        run_id,
        row["trigger_type"],
        meta or {},
        row.get("as_of") or "",
        status or row["status"],
        state,
    )
    return True


def get_run(run_id: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(limit: int = 50) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT run_id, trigger_type, status, as_of, created_at FROM runs "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def save_plan(plan_id: str, ticker: str, status: str, plan: dict):
    now = time.time()
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,COALESCE("
            "(SELECT created_at FROM plans WHERE plan_id=?), ?),?)",
            (plan_id, ticker, status, json.dumps(plan), plan_id, now, now),
        )
        c.commit()


def get_plan(plan_id: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return dict(row) if row else None


def load_plan_body(plan_id: str) -> Optional[dict]:
    """Parse plan JSON from DB; return None if row or payload is missing/invalid."""
    row = get_plan(plan_id)
    if not row:
        return None
    raw = row.get("plan_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


OPEN_PLAN_STATUSES = ("draft", "pending_hitl")
LIVE_PLAN_STATUSES = ("draft", "pending_hitl", "active")
DELETABLE_PLAN_STATUSES = frozenset({"draft", "pending_hitl", "rejected"})


def tickers_with_open_plan_work() -> set[str]:
    """Tickers that already have a draft or pending_hitl plan."""
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT UPPER(ticker) AS t FROM plans "
            "WHERE status IN (?, ?)",
            OPEN_PLAN_STATUSES,
        ).fetchall()
        return {r["t"] for r in rows}


def tickers_with_live_plan_work() -> set[str]:
    """Tickers with draft, pending_hitl, or active plan (blocks duplicate new names)."""
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT UPPER(ticker) AS t FROM plans "
            "WHERE status IN (?, ?, ?)",
            LIVE_PLAN_STATUSES,
        ).fetchall()
        return {r["t"] for r in rows}


def active_plan_for_ticker(ticker: str) -> Optional[dict]:
    """Most recent draft / pending_hitl plan for a ticker, if any."""
    t = ticker.upper().strip()
    with conn() as c:
        row = c.execute(
            "SELECT plan_id, ticker, status, created_at FROM plans "
            "WHERE UPPER(ticker)=? AND status IN (?, ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (t, *OPEN_PLAN_STATUSES),
        ).fetchone()
        return dict(row) if row else None


def canonical_active_plan_for_ticker(ticker: str) -> Optional[dict]:
    """The single authoritative active plan for a ticker (newest if duplicates exist)."""
    t = ticker.upper().strip()
    with conn() as c:
        row = c.execute(
            "SELECT plan_id, ticker, status, created_at FROM plans "
            "WHERE UPPER(ticker)=? AND status=? "
            "ORDER BY created_at DESC LIMIT 1",
            (t, "active"),
        ).fetchone()
        return dict(row) if row else None


def supersede_other_active_plans(
    keep_plan_id: str,
    ticker: str,
    *,
    reason: str = "superseded_by_newer_active_plan",
) -> list[str]:
    """Close other active plans on the same ticker; keep ``keep_plan_id``."""
    t = (ticker or "").upper().strip()
    keep = (keep_plan_id or "").strip()
    if not t or not keep:
        return []
    closed: list[str] = []
    for row in list_plans(status="active"):
        if row["ticker"].upper() != t or row["plan_id"] == keep:
            continue
        update_plan_status(
            row["plan_id"],
            "closed",
            history_append={
                "agent": "system",
                "action": "superseded",
                "note": reason,
                "superseded_by": keep,
            },
        )
        closed.append(row["plan_id"])
    return closed


def rebind_holding_to_plan(ticker: str, plan_id: str) -> bool:
    """Point the holding row at the canonical active plan_id."""
    t = (ticker or "").upper().strip()
    with conn() as c:
        row = c.execute(
            "SELECT * FROM holdings WHERE UPPER(ticker)=?", (t,),
        ).fetchone()
        if not row:
            return False
        h = dict(row)
        c.execute(
            "INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?)",
            (
                h["ticker"], h["quantity"], h["cost_basis"],
                h["current_price"], plan_id, h.get("sector", ""),
            ),
        )
        c.commit()
    return True


def consolidate_duplicate_active_plans() -> dict[str, Any]:
    """
    Ensure at most one active plan per ticker.
    Prefers the plan_id already on the holding row; else newest active.
    """
    from collections import defaultdict

    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for row in list_plans(status="active"):
        by_ticker[row["ticker"].upper()].append(row)

    holding_plan: dict[str, str] = {}
    for h in list_holdings():
        pid = (h.get("plan_id") or "").strip()
        if pid:
            holding_plan[h["ticker"].upper()] = pid

    stats: dict[str, Any] = {
        "tickers_consolidated": 0,
        "plans_closed": 0,
        "keepers": [],
    }
    for ticker, rows in by_ticker.items():
        if len(rows) <= 1:
            continue
        stats["tickers_consolidated"] += 1
        prefer = holding_plan.get(ticker)
        keeper = None
        if prefer:
            keeper = next((r for r in rows if r["plan_id"] == prefer), None)
        if not keeper:
            keeper = max(rows, key=lambda r: float(r.get("created_at") or 0))
        keep_id = keeper["plan_id"]
        if ticker in holding_plan and holding_plan[ticker] != keep_id:
            rebind_holding_to_plan(ticker, keep_id)
        closed = supersede_other_active_plans(
            keep_id, ticker, reason="consolidated_duplicate_active_plans",
        )
        stats["plans_closed"] += len(closed)
        stats["keepers"].append({
            "ticker": ticker,
            "plan_id": keep_id,
            "closed_plan_ids": closed,
        })
    return stats


def dedupe_plans_for_display(plans: list[dict]) -> list[dict]:
    """At most one active plan row per ticker in UI lists."""
    active_best: dict[str, dict] = {}
    rest: list[dict] = []
    for p in plans:
        if p.get("status") != "active":
            rest.append(p)
            continue
        t = (p.get("ticker") or "").upper()
        prev = active_best.get(t)
        if not prev or float(p.get("created_at") or 0) > float(prev.get("created_at") or 0):
            active_best[t] = p
    return rest + list(active_best.values())


def pending_hitl_for_ticker(ticker: str) -> Optional[dict]:
    """Pending HITL queue row for this ticker (any plan), if any."""
    t = ticker.upper().strip()
    with conn() as c:
        row = c.execute(
            "SELECT h.* FROM hitl_queue h "
            "JOIN plans p ON p.plan_id = h.plan_id "
            "WHERE h.status='pending' AND UPPER(p.ticker)=? "
            "ORDER BY h.created_at ASC LIMIT 1",
            (t,),
        ).fetchone()
        return dict(row) if row else None


def list_plans(status: Optional[str] = None) -> list[dict]:
    with conn() as c:
        if status:
            rows = c.execute(
                "SELECT plan_id, ticker, status, created_at FROM plans "
                "WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT plan_id, ticker, status, created_at FROM plans "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def append_plan_history(plan_id: str, entry: dict) -> Optional[dict]:
    """Append a timeline event to plan.history (monitoring, monitor flags, etc.)."""
    plan_data = load_plan_body(plan_id)
    if not plan_data:
        return None
    p = get_plan(plan_id)
    entry = {**entry, "at": entry.get("at") or time.strftime("%Y-%m-%dT%H:%M:%S")}
    plan_data.setdefault("history", []).append(entry)
    row = p or {}
    save_plan(
        plan_id, plan_data.get("ticker", row.get("ticker", "?")),
        plan_data.get("status", row.get("status", "draft")), plan_data,
    )
    return plan_data


def update_plan_status(plan_id: str, new_status: str,
                       approved_by: Optional[str] = None,
                       rejection_reason: Optional[str] = None,
                       history_append: Optional[dict] = None) -> Optional[dict]:
    plan_data = load_plan_body(plan_id)
    if not plan_data:
        return None
    p = get_plan(plan_id)
    plan_data["status"] = new_status
    if approved_by:
        plan_data["approved_by"] = approved_by
        plan_data["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if rejection_reason:
        plan_data["rejection_reason"] = rejection_reason
    if history_append:
        plan_data.setdefault("history", []).append(history_append)
    row = p or {}
    ticker = plan_data.get("ticker") or row.get("ticker", "?")
    if new_status == "active":
        supersede_other_active_plans(
            plan_id, ticker, reason="replaced_by_new_active_plan",
        )
        rebind_holding_to_plan(ticker, plan_id)
    save_plan(plan_id, ticker, new_status, plan_data)
    return plan_data


def list_holdings() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM holdings").fetchall()
        return [dict(r) for r in rows]


def upsert_holding(ticker: str, quantity: int, cost_basis: float,
                   current_price: float, plan_id: str, sector: str):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,?,?)",
            (ticker, quantity, cost_basis, current_price, plan_id, sector),
        )
        c.commit()


def delete_holding(ticker: str) -> bool:
    """Remove an open holding after full exit."""
    with conn() as c:
        cur = c.execute("DELETE FROM holdings WHERE ticker=?", (ticker.upper(),))
        c.commit()
        return cur.rowcount > 0


def journal_append(run_id: str, agent: str, output: dict, duration_ms: int) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO journal (run_id, agent, output_json, duration_ms, ts) "
            "VALUES (?,?,?,?,?)",
            (run_id, agent, json.dumps(output), duration_ms, time.time()),
        )
        c.commit()
        return cur.lastrowid


def list_journal_for_run(run_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM journal WHERE run_id=? ORDER BY journal_id ASC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_audit(about_journal_id: int, severity: str, compliant: bool, note: dict) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO audit_notes (about_journal_id, severity, compliant, note_json, ts) "
            "VALUES (?,?,?,?,?)",
            (about_journal_id, severity, 1 if compliant else 0, json.dumps(note), time.time()),
        )
        c.commit()
        return cur.lastrowid


def audits_for_run(run_id: str) -> list[dict]:
    """Return audits indexed by journal_id for the given run."""
    with conn() as c:
        rows = c.execute(
            "SELECT a.* FROM audit_notes a JOIN journal j ON a.about_journal_id = j.journal_id "
            "WHERE j.run_id=? ORDER BY a.ts ASC", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def enqueue_hitl(run_id: str, plan_id: str) -> Optional[int]:
    """Insert a pending HITL item, or return existing / None when deduped."""
    from . import config

    row = get_plan(plan_id)
    ticker = (row or {}).get("ticker", "")

    with conn() as c:
        existing = c.execute(
            "SELECT * FROM hitl_queue WHERE plan_id=? AND status='pending' "
            "ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if existing:
            return int(existing["item_id"])

        if config.HITL_ONE_PER_TICKER and ticker:
            dup = c.execute(
                "SELECT h.item_id, h.plan_id FROM hitl_queue h "
                "JOIN plans p ON p.plan_id = h.plan_id "
                "WHERE h.status='pending' AND UPPER(p.ticker)=? "
                "LIMIT 1",
                (ticker.upper(),),
            ).fetchone()
            if dup and dup["plan_id"] == plan_id:
                if dup["item_id"]:
                    c.execute(
                        "UPDATE hitl_queue SET run_id=?, created_at=? "
                        "WHERE item_id=?",
                        (run_id, time.time(), dup["item_id"]),
                    )
                    c.commit()
                    return int(dup["item_id"])
            elif dup:
                # One HITL card per ticker — point queue at the plan that needs action now.
                c.execute(
                    "UPDATE hitl_queue SET run_id=?, plan_id=?, created_at=? "
                    "WHERE item_id=? AND status='pending'",
                    (run_id, plan_id, time.time(), dup["item_id"]),
                )
                c.commit()
                return int(dup["item_id"])

        cur = c.execute(
            "INSERT INTO hitl_queue (run_id, plan_id, status, created_at) "
            "VALUES (?,?,?,?)",
            (run_id, plan_id, "pending", time.time()),
        )
        c.commit()
        return cur.lastrowid


def list_hitl_pending() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM hitl_queue WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_hitl_recent(limit: int = 10) -> list[dict]:
    """Recently resolved HITL items (full pipeline auto-approve shows here)."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM hitl_queue WHERE status='resolved' "
            "ORDER BY resolved_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_hitl(item_id: int, resolution: str):
    with conn() as c:
        c.execute(
            "UPDATE hitl_queue SET status='resolved', resolved_at=?, resolution=? "
            "WHERE item_id=?", (time.time(), resolution, item_id),
        )
        c.commit()


def hitl_for_plan(plan_id: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM hitl_queue WHERE plan_id=? ORDER BY created_at DESC LIMIT 1",
            (plan_id,)
        ).fetchone()
        return dict(row) if row else None


def plan_id_from_run_state(state: dict) -> Optional[str]:
    """Extract plan_id from a persisted run state blob."""
    pid = state.get("plan_id")
    if pid:
        return pid
    meta = state.get("trigger_meta") or {}
    if meta.get("plan_id"):
        return meta["plan_id"]
    draft = state.get("plan_draft") or {}
    if draft.get("plan_id"):
        return draft["plan_id"]
    plan = draft.get("plan") or {}
    return plan.get("id")


def recover_stale_running_runs(max_age_sec: int = 900) -> dict:
    """Mark long-stuck ``running`` runs completed so the wait queue stays honest."""
    now = time.time()
    closed = 0
    with conn() as c:
        rows = c.execute(
            "SELECT run_id, trigger_type, state_json, created_at FROM runs "
            "WHERE status='running'",
        ).fetchall()
    for row in rows:
        row = dict(row)
        age = now - float(row.get("created_at") or now)
        if age < max_age_sec:
            continue
        try:
            state = json.loads(row["state_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            state = {}
        state["final_status"] = "completed_stale_running"
        state["cleanup_note"] = (
            f"Run exceeded {max_age_sec}s in running — marked stale by recovery."
        )
        save_run(
            row["run_id"],
            row.get("trigger_type") or state.get("trigger_type", "unknown"),
            state.get("trigger_meta") or {},
            state.get("as_of") or "",
            "completed",
            state,
        )
        closed += 1
    return {"closed_stale_running": closed}


def close_stale_hitl_runs() -> dict:
    """Mark awaiting_hitl runs completed when their plan/HITL row is gone or superseded."""
    pending_plan_ids = {i["plan_id"] for i in list_hitl_pending()}
    closed = 0
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM runs WHERE status='awaiting_hitl'",
        ).fetchall()
    for row in rows:
        row = dict(row)
        try:
            state = json.loads(row["state_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            state = {}
        pid = plan_id_from_run_state(state)
        plan_row = get_plan(pid) if pid else None
        keep = (
            pid in pending_plan_ids
            and plan_row
            and plan_row.get("status") in OPEN_PLAN_STATUSES
        )
        if keep:
            continue
        state["final_status"] = "completed_superseded"
        state["cleanup_note"] = (
            "HITL superseded or plan removed during queue cleanup."
        )
        try:
            meta = json.loads(row["trigger_meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        save_run(
            row["run_id"],
            row["trigger_type"],
            meta,
            row["as_of"],
            "completed",
            state,
        )
        closed += 1
    return {"closed_hitl_runs": closed}


def holding_uses_plan(plan_id: str) -> bool:
    with conn() as c:
        return c.execute(
            "SELECT 1 FROM holdings WHERE plan_id=? LIMIT 1", (plan_id,),
        ).fetchone() is not None


def can_delete_plan(plan_id: str) -> tuple[bool, str]:
    row = get_plan(plan_id)
    if not row:
        return False, "plan not found"
    status = row.get("status") or ""
    if status not in DELETABLE_PLAN_STATUSES:
        return (
            False,
            f"cannot delete plan with status {status!r} "
            f"(only draft, pending_hitl, rejected)",
        )
    if holding_uses_plan(plan_id):
        return False, "plan is linked to a portfolio holding"
    return True, ""


def _close_runs_for_plan(plan_id: str) -> int:
    """Complete runs still paused on a plan that is being removed."""
    closed = 0
    with conn() as c:
        rows = c.execute(
            "SELECT run_id, trigger_type, trigger_meta, as_of, status, state_json "
            "FROM runs WHERE status IN ('awaiting_hitl', 'running')",
        ).fetchall()
    for row in rows:
        row = dict(row)
        try:
            state = json.loads(row["state_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            state = {}
        pid = plan_id_from_run_state(state)
        try:
            meta = json.loads(row["trigger_meta"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if pid != plan_id and meta.get("plan_id") != plan_id:
            continue
        state["final_status"] = "completed_plan_deleted"
        state["cleanup_note"] = f"Plan {plan_id} removed by operator."
        save_run(
            row["run_id"],
            row["trigger_type"],
            meta,
            row.get("as_of") or "",
            "completed",
            state,
        )
        closed += 1
    return closed


def delete_plan(plan_id: str) -> dict:
    """Remove a draft / orphan pending_hitl / rejected plan and unwind HITL + runs."""
    ok, reason = can_delete_plan(plan_id)
    if not ok:
        return {"deleted": False, "plan_id": plan_id, "error": reason}

    resolved_hitl = 0
    hitl = hitl_for_plan(plan_id)
    if hitl and hitl.get("status") == "pending":
        resolve_hitl(int(hitl["item_id"]), "plan_deleted")
        resolved_hitl = 1

    closed_runs = _close_runs_for_plan(plan_id)
    with conn() as c:
        cur = c.execute("DELETE FROM plans WHERE plan_id=?", (plan_id,))
        c.commit()
        deleted = cur.rowcount > 0

    close_stale_hitl_runs()
    return {
        "deleted": deleted,
        "plan_id": plan_id,
        "resolved_hitl": resolved_hitl,
        "closed_runs": closed_runs,
    }


def delete_stuck_plans() -> dict:
    """Delete all draft / rejected plans and pending_hitl without a live queue card."""
    pending_plan_ids = {i["plan_id"] for i in list_hitl_pending()}
    deleted_ids: list[str] = []
    skipped: list[dict] = []
    for row in list_plans():
        pid = row["plan_id"]
        status = row.get("status") or ""
        if status not in DELETABLE_PLAN_STATUSES:
            continue
        if status == "pending_hitl" and pid in pending_plan_ids:
            skipped.append({
                "plan_id": pid,
                "reason": "pending HITL review — resolve or delete from plan page",
            })
            continue
        result = delete_plan(pid)
        if result.get("deleted"):
            deleted_ids.append(pid)
        elif result.get("error"):
            skipped.append({"plan_id": pid, "reason": result["error"]})
    return {
        "deleted_count": len(deleted_ids),
        "deleted_plan_ids": deleted_ids,
        "skipped": skipped,
    }


def delete_plans_by_status(*statuses: str) -> int:
    """Remove plan rows (e.g. superseded rejected clutter). Returns rows deleted."""
    if not statuses:
        return 0
    placeholders = ",".join("?" * len(statuses))
    with conn() as c:
        cur = c.execute(
            f"DELETE FROM plans WHERE status IN ({placeholders})",
            statuses,
        )
        c.commit()
        return cur.rowcount


def purge_plan_clutter() -> dict:
    """Drop rejected plans, stale pending_hitl, and close orphaned awaiting_hitl runs."""
    deleted_rejected = delete_plans_by_status("rejected")
    cleared_pending = 0
    for row in list_plans(status="pending_hitl"):
        pid = row["plan_id"]
        hitl = hitl_for_plan(pid)
        if hitl and hitl.get("status") == "pending":
            continue
        update_plan_status(
            pid, "rejected",
            rejection_reason="Cleared — no active HITL queue item",
        )
        delete_plans_by_status("rejected")
        cleared_pending += 1
    run_stats = close_stale_hitl_runs()
    return {
        "deleted_rejected": deleted_rejected,
        "cleared_orphan_pending_hitl": cleared_pending,
        **run_stats,
    }


def dedupe_pending_hitl_by_ticker() -> dict:
    """Keep oldest pending HITL per ticker; resolve duplicate queue rows and plans."""
    kept: dict[str, dict] = {}
    resolved_items = 0
    rejected_plans = 0
    for item in list_hitl_pending():
        plan_row = get_plan(item["plan_id"])
        if not plan_row:
            resolve_hitl(item["item_id"], "orphan_plan")
            resolved_items += 1
            continue
        ticker = (plan_row.get("ticker") or "?").upper()
        if ticker in kept:
            resolve_hitl(item["item_id"], "superseded_duplicate")
            resolved_items += 1
            if plan_row.get("status") == "pending_hitl":
                update_plan_status(
                    item["plan_id"], "rejected",
                    rejection_reason=(
                        f"Superseded — duplicate work for {ticker}; "
                        f"see {kept[ticker]['plan_id']}"
                    ),
                )
                rejected_plans += 1
        else:
            kept[ticker] = item
    run_stats = close_stale_hitl_runs()
    return {
        "kept_tickers": list(kept.keys()),
        "kept_count": len(kept),
        "resolved_items": resolved_items,
        "rejected_plans": rejected_plans,
        **run_stats,
    }


# ---------- idea_history ----------

def record_idea_history(ticker: str, scan_run_id: str,
                         composite_score: float, recommended_action: str,
                         brief: Optional[dict] = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO idea_history "
            "(ticker, scan_run_id, composite_score, recommended_action, brief_json, suggested_at) "
            "VALUES (?,?,?,?,?,?)",
            (ticker, scan_run_id, float(composite_score or 0),
             recommended_action, json.dumps(brief or {}), time.time()),
        )
        c.commit()
        return cur.lastrowid


def get_idea_history_for_ticker(ticker: str) -> list[dict]:
    """All times this ticker was suggested, newest first."""
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM idea_history WHERE ticker=? "
            "ORDER BY suggested_at DESC", (ticker,)
        ).fetchall()
        return [dict(r) for r in rows]


def all_suggested_tickers() -> set[str]:
    with conn() as c:
        rows = c.execute(
            "SELECT DISTINCT ticker FROM idea_history"
        ).fetchall()
        return {r["ticker"] for r in rows}


_ROUTING_ACTIONS = ("open_new_research", "add_to_existing")


def recently_suggested_tickers(window_seconds: float = 7 * 24 * 3600) -> set[str]:
    """Tickers routed to research/plan within the past N seconds.

    Watch-list entries are intentionally excluded — recording every `watch`
    would empty the scan universe after a single run.
    """
    cutoff = time.time() - window_seconds
    placeholders = ",".join("?" * len(_ROUTING_ACTIONS))
    with conn() as c:
        rows = c.execute(
            f"SELECT DISTINCT ticker FROM idea_history "
            f"WHERE suggested_at >= ? AND recommended_action IN ({placeholders})",
            (cutoff, *_ROUTING_ACTIONS),
        ).fetchall()
        return {r["ticker"] for r in rows}


def list_idea_history_for_run(scan_run_id: str) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM idea_history WHERE scan_run_id=? "
            "ORDER BY composite_score DESC", (scan_run_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def trade_history_count() -> int:
    with conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM trade_history").fetchone()
        return int(row["n"]) if row else 0


def insert_trade(trade: dict) -> None:
    meta = trade.get("meta")
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO trade_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade["trade_id"],
                float(trade.get("ts") or time.time()),
                trade.get("as_of") or "",
                trade["ticker"],
                trade.get("side") or "long",
                trade["action"],
                int(trade.get("quantity") or 0),
                float(trade.get("price") or 0),
                float(trade.get("notional_usd") or 0),
                trade.get("plan_id") or "",
                trade.get("run_id") or "",
                trade.get("sector") or "",
                trade.get("source") or "live",
                json.dumps(meta) if meta is not None else "{}",
            ),
        )
        c.commit()


def list_trade_history(limit: int = 200) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM trade_history ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["meta"] = {}
        out.append(d)
    return out
