"""Market-hours cadence — pre-open prep, open routing, supervision, EOD report."""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from . import config, db, firm_state as fs_mod, graph, rag, traces, trade_history
from .agents import firm_manager
from .core.logging import get_logger
from .daily_plan import load, mark_job_done, save, set_eod_report, set_manager_brief
from .market_calendar import JOB_SPECS, is_equity_session_open

log = get_logger("horizon.cadence")

# Prometheus-friendly counter (in-process; scraped via logs/metrics extension)
_cadence_runs: dict[str, int] = {}


def _cadence_skip_hours() -> bool:
    """Allow cadence jobs outside session (dev/replay)."""
    return bool(getattr(config, "SKIP_MARKET_HOURS_CADENCE", False))


def cadence_metrics() -> dict[str, Any]:
    return {"jobs_completed": dict(_cadence_runs)}


def run_job(job_id: str) -> dict[str, Any]:
    """Execute one cadence job. Idempotent per day via daily_plan.completed_jobs."""
    plan = load()
    if job_id in (plan.get("completed_jobs") or []):
        log.info("cadence-skip-already-done job=%s", job_id)
        return {"job_id": job_id, "skipped": True, "reason": "already_completed"}

    spec = JOB_SPECS.get(job_id)
    if not spec:
        raise ValueError(f"unknown cadence job: {job_id}")

    log.info("cadence-start job=%s label=%s", job_id, spec.label)
    traces.record("cadence_job_start", {"job_id": job_id, "label": spec.label})

    try:
        if job_id == "pre_open":
            result = _run_pre_open()
        elif job_id == "market_open":
            result = _run_market_open()
        elif job_id == "mid_morning":
            result = _run_mid_morning()
        elif job_id == "pre_close":
            result = _run_pre_close()
        elif job_id == "eod":
            result = _run_eod()
        else:
            result = {"summary": "noop"}
    except Exception as e:
        log.exception("cadence-failed job=%s", job_id)
        traces.record("cadence_job_error", {"job_id": job_id, "error": str(e)})
        from . import metrics_registry, ops_alerts
        metrics_registry.observe_cadence_job(job_id, ok=False)
        ops_alerts.record(
            code=f"cadence_{job_id}_failed",
            message=str(e),
            severity="error",
            source="daily_cadence",
            context={"job_id": job_id},
        )
        raise

    mark_job_done(job_id, result=result)
    _cadence_runs[job_id] = _cadence_runs.get(job_id, 0) + 1
    from . import metrics_registry
    metrics_registry.observe_cadence_job(job_id, ok=True)
    traces.record("cadence_job_end", {"job_id": job_id, **result})
    log.info("cadence-done job=%s summary=%s", job_id, result.get("summary"))
    return result


def _run_pre_open() -> dict[str, Any]:
    """07:30 ET — surface integrity, seed manager brief, no trading."""
    rag_bootstrap = __import__("app.rag_bootstrap", fromlist=["ensure_ready"]).ensure_ready
    rag_bootstrap()

    as_of = time.strftime("%Y-%m-%dT%H:%M:%S")
    run_id = "cad_pre_" + uuid.uuid4().hex[:10]
    book = fs_mod.build_firm_state(refresh_prices=False)
    traces.set_context(run_id=run_id)
    traces.record("run_start", {"trigger_type": "cadence_pre_open"})

    mgr_out, _, _ = graph._run_agent(
        run_id, "firm_manager", firm_manager.run, book, as_of,
    )
    db.save_run(
        run_id,
        "cadence_pre_open",
        {"phase": "pre_open", "read_only": True},
        as_of,
        "completed",
        {
            "run_id": run_id,
            "trigger_type": "cadence_pre_open",
            "firm_manager": mgr_out,
            "rag_counts": {
                "policy": rag.count("policy"),
                "news": rag.count("news"),
                "filings": rag.count("filings"),
                "past_plans": rag.count("past_plans"),
            },
            "pending_hitl": len(db.list_hitl_pending()),
            "open_plans": len(db.list_plans()),
        },
    )
    traces.record("run_end", {"final_status": "completed"})
    set_manager_brief(mgr_out, phase="pre_open")

    plan = load()
    plan["integrity"] = {
        "rag_ready": True,
        "pending_hitl": len(db.list_hitl_pending()),
        "holdings": len(db.list_holdings()),
        "recent_runs": len(db.list_runs(10)),
    }
    save(plan)

    return {
        "summary": "pre_open_integrity",
        "run_id": run_id,
        "tasks": len(mgr_out.get("tasks") or []),
    }


def _run_market_open() -> dict[str, Any]:
    """09:35 ET — manager routing + supervised execution during session."""
    if not is_equity_session_open() and not _cadence_skip_hours():
        return {"summary": "skipped_outside_session", "spawned": 0}

    from . import firm_orchestration

    auto_exec = config.CADENCE_MARKET_OPEN_AUTO_EXECUTE
    run_id = firm_orchestration.begin_balance_cycle(
        trigger_supervision=True,
        spawn_pipeline=True,
        force_scan=config.FIRM_MANAGER_BALANCE_FORCE_SCAN,
        cadence_phase="market_open",
        auto_execute=auto_exec,
    )

    state = firm_orchestration.execute_balance_cycle(
        run_id,
        trigger_supervision=True,
        spawn_pipeline=True,
    )
    # Supervision may have been started inside balance; re-run with auto_execute if configured
    if auto_exec and config.AUTO_PLAN_SUPERVISION:
        sup = graph.start_plan_supervision(
            spawn_pipeline=True,
            auto_execute=True,
            manager_out=state.get("firm_manager"),
            skip_orchestration=True,
        )
        spawned = len(sup.state.get("spawned_run_ids") or [])
    else:
        spawned = len(state.get("spawned_run_ids") or [])

    mgr = state.get("firm_manager") or {}
    set_manager_brief(mgr, phase="market_open")
    return {
        "summary": "market_open_balance",
        "run_id": run_id,
        "spawned": spawned,
        "auto_execute": auto_exec,
    }


def _run_mid_morning() -> dict[str, Any]:
    """10:00 ET — drift supervision (spawn pipelines, no auto-execute)."""
    if not is_equity_session_open() and not _cadence_skip_hours():
        return {"summary": "skipped_outside_session"}

    sup = graph.start_plan_supervision(
        spawn_pipeline=config.AUTO_PLAN_SPAWN_PIPELINE,
        auto_execute=False,
    )
    return {
        "summary": "mid_morning_supervision",
        "run_id": sup.run_id,
        "spawned": len(sup.state.get("spawned_run_ids") or []),
    }


def _run_pre_close() -> dict[str, Any]:
    """15:30 ET — health check only (supervision without new spawns)."""
    sup = graph.start_plan_supervision(
        spawn_pipeline=False,
        auto_execute=False,
    )
    return {"summary": "pre_close_health", "run_id": sup.run_id}


def _run_eod() -> dict[str, Any]:
    """16:35 ET — Excel + JSON report and next-day manager brief."""
    from .reports import build_daily_report, write_daily_report_xlsx
    from .reports.__main__ import _gather_live_data

    data = _gather_live_data()
    rep = build_daily_report(**data)
    path = write_daily_report_xlsx(rep)
    set_eod_report(str(path), rep.as_dict())
    traces.record("report_rendered", {
        "path": str(path),
        "window": rep.window,
        "nav": rep.nav,
    })

    as_of = time.strftime("%Y-%m-%dT%H:%M:%S")
    run_id = "cad_eod_" + uuid.uuid4().hex[:10]
    book = fs_mod.build_firm_state(refresh_prices=True)
    traces.set_context(run_id=run_id)
    mgr_out, _, _ = graph._run_agent(
        run_id, "firm_manager", firm_manager.run, book, as_of,
    )
    db.save_run(
        run_id,
        "cadence_eod",
        {"phase": "eod", "report_path": str(path)},
        as_of,
        "completed",
        {
            "run_id": run_id,
            "trigger_type": "cadence_eod",
            "firm_manager": mgr_out,
            "daily_report": rep.as_dict(),
            "trades_today": len(trade_history.get_firm_trade_history(limit=500)),
        },
    )
    set_manager_brief(mgr_out, phase="eod_next_day")

    plan = load()
    plan["next_day_brief"] = {
        "generated_at": as_of,
        "tasks": plan.get("next_day_tasks") or [],
        "book_summary": mgr_out.get("book_summary"),
    }
    save(plan)

    return {
        "summary": "eod_reconciliation",
        "report_path": str(path),
        "run_id": run_id,
        "nav": rep.nav,
        "pnl_pct": rep.pnl_pct,
    }
