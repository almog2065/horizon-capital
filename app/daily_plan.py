"""Persisted daily operations plan — schedule, manager brief, EOD artifacts."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from . import ops_db
from .core.logging import get_logger
from .market_calendar import build_day_schedule, trading_date

log = get_logger("horizon.daily_plan")


def plan_path(for_date: Optional[str] = None) -> Path:
    """Logical storage key (data lives in ``ops.sqlite``)."""
    day = for_date or trading_date().isoformat()
    return ops_db.plan_storage_ref(day)


def _empty_plan(for_date: Optional[str] = None) -> dict[str, Any]:
    day = for_date or trading_date().isoformat()
    return {
        "date": day,
        "created_at": time.time(),
        "updated_at": time.time(),
        "schedule": build_day_schedule(),
        "completed_jobs": [],
        "manager_brief": None,
        "next_day_tasks": [],
        "eod_report_path": None,
        "runs_today": [],
        "notes": [],
    }


def load(for_date: Optional[str] = None) -> dict[str, Any]:
    ops_db.init_db()
    day = for_date or trading_date().isoformat()
    stored = ops_db.load_daily_plan(day)
    if stored is None:
        return _empty_plan(day)
    return stored


def save(plan: dict[str, Any]) -> Path:
    ops_db.init_db()
    plan["updated_at"] = time.time()
    day = plan.get("date") or trading_date().isoformat()
    ops_db.save_daily_plan(day, plan)
    path = plan_path(day)
    log.info("daily-plan-saved db=ops.sqlite date=%s", day)
    return path


def mark_job_done(job_id: str, *, result: Optional[dict] = None) -> dict[str, Any]:
    plan = load()
    done = set(plan.get("completed_jobs") or [])
    done.add(job_id)
    plan["completed_jobs"] = sorted(done)
    for row in plan.get("schedule") or []:
        if row.get("job_id") == job_id:
            row["status"] = "done"
            if result:
                row["result_summary"] = result.get("summary", "ok")
    history = plan.setdefault("job_history", [])
    history.append({
        "job_id": job_id,
        "ts": time.time(),
        "result": result or {},
    })
    save(plan)
    return plan


def set_manager_brief(mgr: dict, *, phase: str = "") -> dict[str, Any]:
    plan = load()
    plan["manager_brief"] = {
        "phase": phase,
        "book_summary": mgr.get("book_summary"),
        "tasks": (mgr.get("tasks") or [])[:12],
        "scan_directives": mgr.get("scan_directives"),
        "supervision_focus": mgr.get("supervision_focus") or [],
        "reasoning_narrative": (mgr.get("reasoning_narrative") or "")[:2000],
    }
    plan["next_day_tasks"] = [
        {
            "type": t.get("type"),
            "ticker": t.get("ticker"),
            "priority": t.get("priority"),
            "rationale": (t.get("rationale") or "")[:200],
        }
        for t in (mgr.get("tasks") or [])[:8]
    ]
    save(plan)
    return plan


def set_eod_report(path: str, report: dict) -> dict[str, Any]:
    plan = load()
    plan["eod_report_path"] = path
    plan["eod_summary"] = {
        "nav": report.get("nav"),
        "pnl_pct": report.get("pnl_pct"),
        "benchmark_pct": report.get("benchmark_pct"),
        "n_trades": len(report.get("trades") or []),
    }
    save(plan)
    return plan


def operations_view() -> dict[str, Any]:
    """Bundle for dashboard: schedule + completion + next event."""
    from .market_calendar import (
        due_jobs,
        is_equity_session_open,
        next_job_utc,
        seconds_until_next_event,
        to_et,
    )

    plan = load()
    completed = set(plan.get("completed_jobs") or [])
    now = time.time()
    nxt = next_job_utc(completed=completed)
    return {
        "plan": plan,
        "schedule": plan.get("schedule") or build_day_schedule(completed=completed),
        "completed_jobs": sorted(completed),
        "due_now": due_jobs(completed=completed),
        "next_job": {"id": nxt[0], "at_utc": nxt[1].isoformat()} if nxt else None,
        "seconds_until_next": seconds_until_next_event(completed=completed),
        "et_now": to_et().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "session_open": is_equity_session_open(),
        "now": now,
    }
