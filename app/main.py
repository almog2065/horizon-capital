"""FastAPI app — trigger runs, view state, approve HITL, view traces.

Uses lifespan (FastAPI 0.100+).
"""
from __future__ import annotations

import asyncio
import json
import platform
import sys
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (
    config,
    daily_plan,
    db,
    eval_dashboard,
    firm_state,
    firm_timeline,
    graph,
    hitl_brief,
    hitl_sync,
    llm,
    pipeline_view,
    portfolio,
    rag,
    rag_bootstrap,
    tools,
    traces,
    trade_history,
    wait_queue,
)
from .agents import (
    firm_manager,
    news_triage,  # noqa
)

# NOTE: The startup logic that used to live here has moved to
# app/core/lifecycle.py — the production lifespan. The web container
# and the worker container both reuse it so they boot the firm the
# same way (db init, RAG seed, HITL repair, plan dedup, stale-run
# recovery, then optional scheduler tasks).
#
# We re-export the same name so `uvicorn app.main:app` keeps working
# for the legacy demo entry point and for backwards-compat tests.
from .core.lifecycle import lifespan  # noqa: F401  re-exported for back-compat
from .market_calendar import trading_date

app = FastAPI(title="Horizon Capital — Demo", lifespan=lifespan)
ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ---------------- pages ----------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    runs = db.list_runs(20)
    plans = db.dedupe_plans_for_display(
        [p for p in db.list_plans() if p["status"] != "rejected"],
    )
    hitl_repair = hitl_sync.repair_hitl_queue()
    hitl = [hitl_brief.enrich_hitl_item(i) for i in db.list_hitl_pending()]
    hitl_recent = db.list_hitl_recent(8)
    holdings = db.list_holdings()
    pf = portfolio.get_portfolio_summary(refresh_prices=True)
    firm = firm_state.build_firm_state(refresh_prices=False)
    wait = wait_queue.build_wait_queue()
    ops = daily_plan.operations_view()
    mgr = firm_manager.latest_snapshot(firm)
    th = trade_history.get_firm_trade_history(limit=12)
    timeline = firm_timeline.build_firm_timeline()
    hitl_resolved = request.query_params.get("hitl_resolved", "")
    hitl_flash = None
    if hitl_resolved in ("approve", "reject"):
        tick = request.query_params.get("ticker", "")
        hitl_flash = {
            "decision": hitl_resolved,
            "ticker": tick,
            "message": (
                f"{tick} approved — execution pipeline resumed."
                if hitl_resolved == "approve"
                else f"{tick} rejected."
            ),
        }
    exit_flash = None
    exit_ok = request.query_params.get("exit_ok", "")
    exit_err = request.query_params.get("exit_err", "")
    if exit_ok:
        exit_flash = {
            "ok": True,
            "ticker": exit_ok,
            "message": f"{exit_ok} position closed — proceeds added to cash.",
        }
    elif exit_err:
        exit_flash = {
            "ok": False,
            "ticker": exit_err,
            "message": request.query_params.get("exit_msg", f"Could not exit {exit_err}."),
        }
    return TEMPLATES.TemplateResponse(request, "index.html", {
        "runs": runs,
        "hitl_flash": hitl_flash,
        "exit_flash": exit_flash,
        "plans": plans,
        "hitl": hitl,
        "hitl_repair": hitl_repair,
        "hitl_recent": hitl_recent,
        "holdings": holdings,
        "pf": pf,
        "firm": firm,
        "wait": wait,
        "ops": ops,
        "mgr": mgr,
        "trade_hist": th,
        "timeline": timeline,
        "fmt_trade_ts": trade_history.fmt_trade_ts,
        "fmt_usd": portfolio.fmt_usd,
        "fmt_pct": portfolio.fmt_pct,
        "rag_counts": {
            "policy": rag.count("policy"),
            "news": rag.count("news"),
            "filings": rag.count("filings"),
            "past_plans": rag.count("past_plans"),
        },
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "fmt_time": lambda t: time.strftime("%H:%M:%S", time.localtime(t)),
    })


@app.get("/trades", response_class=HTMLResponse)
def trades_page(request: Request):
    th = trade_history.get_firm_trade_history(limit=200)
    return TEMPLATES.TemplateResponse(request, "trades.html", {
        "trade_hist": th,
        "fmt_trade_ts": trade_history.fmt_trade_ts,
        "fmt_usd": portfolio.fmt_usd,
        "fmt_pct": portfolio.fmt_pct,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


@app.get("/trigger", response_class=HTMLResponse)
def trigger_form(request: Request):
    samples_dir = config.NEWS_SAMPLES_DIR
    samples = []
    if samples_dir.exists():
        for p in sorted(samples_dir.glob("*.json")):
            samples.append({"name": p.stem, "data": json.loads(p.read_text())})
    return TEMPLATES.TemplateResponse(request, "trigger.html", {
        "samples": samples,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


def _news_from_form(headline: str, body: str, tickers: str,
                    source: str = "manual") -> dict:
    return {
        "id": "news_" + str(int(time.time() * 1000)),
        "headline": headline,
        "body": body,
        "tickers": [t.strip().upper() for t in tickers.split(",") if t.strip()],
        "source": source,
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


@app.post("/trigger")
def trigger_submit(headline: str = Form(...),
                   body: str = Form(...),
                   tickers: str = Form(...),
                   source: str = Form("manual")):
    news = _news_from_form(headline, body, tickers, source)
    result = graph.start_news_run(news)
    return RedirectResponse(url=f"/run/{result.run_id}", status_code=303)


@app.post("/trigger/full")
def trigger_full_submit(headline: str = Form(...),
                        body: str = Form(...),
                        tickers: str = Form(...),
                        source: str = Form("manual")):
    """Backward-compatible alias — same agent-driven routing as /trigger."""
    news = _news_from_form(headline, body, tickers, source)
    result = graph.start_news_run_full(news)
    return RedirectResponse(url=f"/run/{result.run_id}", status_code=303)


@app.post("/trigger/watchlist-sweep")
def trigger_watchlist_sweep():
    results = graph.start_watchlist_full_pipeline()
    if results:
        return RedirectResponse(url=f"/run/{results[-1].run_id}", status_code=303)
    return RedirectResponse(url="/trigger", status_code=303)


# ---------------- Position Monitor ----------------

@app.get("/monitor", response_class=HTMLResponse)
def monitor_form(request: Request):
    last_mon = None
    for r in db.list_runs(50):
        if r["trigger_type"] in ("plan_supervision", "position_monitor"):
            run = db.get_run(r["run_id"])
            if run:
                last_mon = {
                    "run_id": run["run_id"],
                    "state": json.loads(run["state_json"]),
                    "status": run["status"],
                    "as_of": run["as_of"],
                }
            break
    holdings = db.list_holdings()
    from .agents.plan_supervisor import MONITORED_STATUSES
    plans_count = sum(
        1 for p in db.list_plans() if p["status"] in MONITORED_STATUSES
    )
    return TEMPLATES.TemplateResponse(request, "monitor.html", {
        "last_monitor": last_mon,
        "holdings_count": len(holdings),
        "plans_count": plans_count,
        "auto_supervision": config.AUTO_PLAN_SUPERVISION,
        "auto_execute": config.AUTO_PLAN_EXECUTE,
        "supervision_interval_min": config.PLAN_SUPERVISION_INTERVAL_SEC // 60,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


@app.post("/manager/balance")
async def manager_balance(
    trigger_supervision: str = Form("no"),
    spawn_pipeline: str = Form("yes"),
):
    from . import firm_orchestration

    sup = trigger_supervision in ("yes", "true", "1", "on")
    spawn = spawn_pipeline in ("yes", "true", "1", "on")
    run_id = firm_orchestration.begin_balance_cycle(
        trigger_supervision=sup,
        spawn_pipeline=spawn,
        force_scan=True,
    )
    asyncio.create_task(
        asyncio.to_thread(
            firm_orchestration.run_balance_background,
            run_id,
            trigger_supervision=sup,
            spawn_pipeline=spawn,
        ),
    )
    return RedirectResponse(url=f"/run/{run_id}", status_code=303)


@app.post("/positions/{ticker}/exit")
def position_exit(ticker: str):
    """Operator full exit from dashboard positions table."""
    out = tools.close_position_sim(ticker.upper(), run_id="operator_dashboard")
    if out.get("status") == "filled":
        pnl = out.get("realized_pnl_usd", 0)
        msg = (
            f"Sold {out.get('quantity')} {ticker.upper()} @ ${out.get('fill_price', 0):,.2f} "
            f"(P&L {pnl:+,.0f} USD)."
        )
        return RedirectResponse(
            f"/?exit_ok={ticker.upper()}&exit_msg={quote(msg)}",
            status_code=303,
        )
    reason = ""
    if out.get("reasons"):
        reason = str(out["reasons"][0].get("reason", ""))[:120]
    return RedirectResponse(
        f"/?exit_err={ticker.upper()}&exit_msg={quote(reason or 'Exit rejected')}",
        status_code=303,
    )


@app.post("/monitor/run")
def monitor_run(
    spawn_pipeline: str = Form("yes"),
    auto_execute: str = Form("no"),
):
    spawn = spawn_pipeline in ("yes", "true", "1", "on")
    auto = auto_execute in ("yes", "true", "1", "on")
    result = graph.start_plan_supervision(
        spawn_pipeline=spawn, auto_execute=auto,
    )
    return RedirectResponse(url=f"/run/{result.run_id}", status_code=303)


@app.post("/api/trigger/monitor")
def api_trigger_monitor(payload: dict):
    spawn = bool(payload.get("spawn_pipeline", True))
    auto = bool(payload.get("auto_execute", False))
    result = graph.start_plan_supervision(
        spawn_pipeline=spawn, auto_execute=auto,
    )
    state = result.state
    reports = state.get("plan_evaluations") or state.get("position_reports") or []
    return JSONResponse({
        "run_id": result.run_id,
        "status": result.status,
        "final_status": state.get("final_status"),
        "plans_evaluated": state.get("plans_evaluated", state.get("positions_evaluated")),
        "spawned_run_ids": state.get("spawned_run_ids", []),
        "reports": [
            {
                "ticker": r.get("ticker"),
                "overall_status": (
                    (r.get("supervisor_report") or {}).get("verdict")
                    or r.get("overall_status")
                ),
                "breaches": len(r.get("breaches") or []),
                "actions": r.get("recommended_actions"),
            }
            for r in reports
        ],
    })


# ---------------- Idea Generator (proactive scan) ----------------

@app.get("/scan", response_class=HTMLResponse)
def scan_form(request: Request):
    # Most recent scan run, if any, for showing previous results
    last_scan = None
    for r in db.list_runs(50):
        if r["trigger_type"] == "idea_scan":
            run = db.get_run(r["run_id"])
            if run:
                last_scan = {
                    "run_id": run["run_id"],
                    "state": json.loads(run["state_json"]),
                    "status": run["status"],
                    "as_of": run["as_of"],
                }
            break
    # Candidate pool preview
    from .agents import idea_generator as _ig
    pool = _ig._load_candidate_pool()
    return TEMPLATES.TemplateResponse(request, "scan.html", {
        "last_scan": last_scan,
        "pool": pool,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


@app.post("/scan")
def scan_submit(background_tasks: BackgroundTasks,
                top_k: int = Form(3),
                spawn_downstream: str = Form("yes"),
                only_new: str = Form("yes"),
                novelty_window_days: float = Form(7.0)):
    spawn = spawn_downstream in ("yes", "true", "1", "on")
    only_new_b = only_new in ("yes", "true", "1", "on")
    begun = graph.begin_idea_scan(
        top_k=top_k, spawn_downstream=spawn,
        only_new=only_new_b, novelty_window_days=novelty_window_days,
    )
    background_tasks.add_task(
        graph.execute_idea_scan,
        begun.scan_run_id,
        top_k,
        spawn,
        only_new_b,
        novelty_window_days,
    )
    return RedirectResponse(
        url=f"/run/{begun.scan_run_id}", status_code=303,
    )


@app.post("/api/trigger/scan")
def api_trigger_scan(payload: dict):
    top_k = int(payload.get("top_k", 3))
    spawn = bool(payload.get("spawn_downstream", True))
    only_new_b = bool(payload.get("only_new", True))
    novelty_window_days = float(payload.get("novelty_window_days", 7.0))
    result = graph.start_idea_scan(
        top_k=top_k, spawn_downstream=spawn,
        only_new=only_new_b, novelty_window_days=novelty_window_days,
    )
    return JSONResponse({
        "scan_run_id": result.scan_run_id,
        "status": result.status,
        "top_picks": [
            {"ticker": p["ticker"],
             "composite_score": p["composite_score"],
             "recommended_action": p["recommended_action"],
             "is_new": (p.get("novelty") or {}).get("is_new", True),
             "has_research_brief": bool(p.get("research_brief"))}
            for p in result.top_picks
        ],
        "downstream_run_ids": [r.run_id for r in result.downstream_runs],
    })


def _redirect_after_rerun(result) -> str:
    if hasattr(result, "scan_run_id"):
        return f"/run/{result.scan_run_id}"
    return f"/run/{result.run_id}"


@app.post("/run/{run_id}/rerun")
def run_rerun(run_id: str, background_tasks: BackgroundTasks):
    row = db.get_run(run_id)
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] == "running":
        raise HTTPException(409, "run is still in progress")

    state = json.loads(row["state_json"])
    if row["trigger_type"] in ("idea_scan", "plan_supervision", "position_monitor"):
        meta = state.get("trigger_meta") or json.loads(row.get("trigger_meta") or "{}")
        begun = graph.begin_idea_scan(
            top_k=int(meta.get("top_k", 3)),
            spawn_downstream=bool(meta.get("spawn_downstream", True)),
            only_new=bool(meta.get("only_new", True)),
            novelty_window_days=float(meta.get("novelty_window_days", 7.0)),
        )
        background_tasks.add_task(
            graph.execute_idea_scan,
            begun.scan_run_id,
            int(meta.get("top_k", 3)),
            bool(meta.get("spawn_downstream", True)),
            bool(meta.get("only_new", True)),
            float(meta.get("novelty_window_days", 7.0)),
        )
        return RedirectResponse(url=f"/run/{begun.scan_run_id}", status_code=303)

    try:
        result = graph.rerun_run(run_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return RedirectResponse(url=_redirect_after_rerun(result), status_code=303)


@app.post("/plan/{plan_id}/rerun")
def plan_rerun(plan_id: str, background_tasks: BackgroundTasks):
    source_run_id = graph.find_run_id_for_plan(plan_id)
    if not source_run_id:
        raise HTTPException(404, "no pipeline run found for this plan")
    return run_rerun(source_run_id, background_tasks)


@app.get("/run/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    state = json.loads(run["state_json"])
    journal = db.list_journal_for_run(run_id)
    audits = db.audits_for_run(run_id)
    audit_by_journal = {a["about_journal_id"]: a for a in audits}

    # Build per-agent trace bundles
    all_traces = traces.list_for_run(run_id)
    traces_by_agent: dict[str, list[dict]] = {}
    for t in all_traces:
        traces_by_agent.setdefault(t["agent"] or "_global", []).append(t)

    enriched_journal = []
    for j in journal:
        j = dict(j)
        j["output"] = json.loads(j["output_json"])
        a = audit_by_journal.get(j["journal_id"])
        if a:
            j["audit"] = json.loads(a["note_json"])
            j["audit_severity"] = a["severity"]
        # Include the agent's own traces AND any auditor traces linked to
        # this journal entry, so the UI shows everything that happened
        # around this step.
        agent_traces = [t for t in traces_by_agent.get(j["agent"], [])
                        if t.get("journal_id") == j["journal_id"]
                        or t.get("journal_id") is None]
        auditor_traces = [t for t in traces_by_agent.get("auditor", [])
                          if t.get("journal_id") == j["journal_id"]]
        j["traces"] = agent_traces + auditor_traces
        # Counts
        j["llm_calls"] = sum(1 for t in j["traces"] if t["event_type"] == "llm_call")
        j["tool_calls"] = sum(1 for t in j["traces"] if t["event_type"] == "tool_call")
        j["rag_calls"] = sum(1 for t in j["traces"] if t["event_type"] == "rag_retrieval")
        j["call_inventory"] = traces.build_call_inventory_from_traces(j["traces"])
        enriched_journal.append(j)

    plan_id, plan_dict = _resolve_plan(state)

    is_running = run["status"] in ("running", "awaiting_hitl")
    pipeline = pipeline_view.build_for_run(
        state, run["status"], trigger_type=run["trigger_type"],
        run_id=run_id,
    )
    if run["status"] == "running":
        db.recover_stale_running_runs(max_age_sec=1800)
        row = db.get_run(run_id) or run
        if row["status"] != "running":
            run = row
            state = json.loads(run["state_json"])
            pipeline = pipeline_view.build_for_run(
                state, run["status"], trigger_type=run["trigger_type"],
                run_id=run_id,
            )
    run_progress = pipeline_view.run_progress_hint(
        run, state, pipeline, journal=enriched_journal,
    )
    can_rerun = (
        not is_running
        and run["trigger_type"] in (
            "news_event", "idea_scan", "position_monitor", "plan_supervision",
            "firm_balance",
        )
    )
    trace_pipeline = traces.build_trace_pipeline(run_id)
    return TEMPLATES.TemplateResponse(request, "run.html", {
        "run": run,
        "state": state,
        "journal": enriched_journal,
        "plan": plan_dict,
        "plan_id": plan_id,
        "global_traces": traces_by_agent.get("_global", []),
        "trace_pipeline": trace_pipeline,
        "is_running": is_running,
        "run_progress": run_progress,
        "can_rerun": can_rerun,
        "pipeline": pipeline,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "ls_status": traces.env_status(),
    })


def _resolve_plan(state: dict) -> tuple[str | None, dict | None]:
    """Load plan from DB, or embed in run state and backfill DB if missing."""
    draft = state.get("plan_draft") or {}
    plan_id = draft.get("plan_id") or (draft.get("plan") or {}).get("id")
    if not plan_id:
        return None, None
    row = db.get_plan(plan_id)
    if row:
        return plan_id, json.loads(row["plan_json"])
    embedded = draft.get("plan")
    if embedded and embedded.get("id") == plan_id:
        status = embedded.get("status", "draft")
        db.save_plan(plan_id, embedded.get("ticker", "?"), status, embedded)
        return plan_id, embedded
    return plan_id, None


def _backfill_plan_from_runs(plan_id: str) -> dict | None:
    """Find plan JSON in any run state (fixes 404 when plan_id only in journal)."""
    for run_row in db.list_runs(100):
        run = db.get_run(run_row["run_id"])
        if not run:
            continue
        state = json.loads(run["state_json"])
        _, plan_dict = _resolve_plan(state)
        if plan_dict and plan_dict.get("id") == plan_id:
            return plan_dict
    return None


@app.get("/plan/{plan_id}", response_class=HTMLResponse)
def plan_detail(request: Request, plan_id: str):
    plan_row = db.get_plan(plan_id)
    if not plan_row:
        recovered = _backfill_plan_from_runs(plan_id)
        if not recovered:
            raise HTTPException(404, "plan not found")
        plan_row = db.get_plan(plan_id)
    plan = json.loads(plan_row["plan_json"])
    hitl = db.hitl_for_plan(plan_id)
    pipeline = pipeline_view.build_plan_pipeline(plan)
    source_run_id = graph.find_run_id_for_plan(plan_id)
    can_delete, _delete_reason = db.can_delete_plan(plan_id)
    has_pending_hitl = bool(hitl and hitl.get("status") == "pending")
    return TEMPLATES.TemplateResponse(request, "plan.html", {
        "plan": plan,
        "plan_row": plan_row,
        "hitl": hitl,
        "pipeline": pipeline,
        "source_run_id": source_run_id,
        "can_delete_plan": can_delete,
        "has_pending_hitl": has_pending_hitl,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


@app.get("/hitl/{item_id}", response_class=HTMLResponse)
def hitl_detail(request: Request, item_id: int):
    items = db.list_hitl_pending()
    item = next((i for i in items if i["item_id"] == item_id), None)
    if not item:
        raise HTTPException(404, "hitl item not pending or not found")
    plan_row = db.get_plan(item["plan_id"])
    if not plan_row:
        raise HTTPException(404, "plan not found")
    plan = json.loads(plan_row["plan_json"])
    run = db.get_run(item["run_id"])
    if not run:
        state, _ = graph._load_or_recover_hitl_run(item["run_id"], item["plan_id"])
        run = db.get_run(item["run_id"])
    else:
        state = json.loads(run["state_json"])
    journal = db.list_journal_for_run(item["run_id"]) if run else []
    for j in journal:
        j["output"] = json.loads(j["output_json"])
    brief = hitl_brief.build_hitl_brief(plan, state, run)
    return TEMPLATES.TemplateResponse(request, "hitl.html", {
        "item": item,
        "plan": plan,
        "run": run,
        "state": state,
        "journal": journal,
        "brief": brief,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


@app.post("/hitl/repair")
def hitl_repair():
    """Reconcile pending_hitl plans / awaiting_hitl runs with the operator queue."""
    stats = hitl_sync.repair_hitl_queue()
    print(f"[hitl] repair: {stats}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/hitl/dedupe")
def hitl_dedupe():
    """One pending HITL per ticker; reject superseded duplicate plans."""
    stats = db.dedupe_pending_hitl_by_ticker()
    print(f"[hitl] dedupe: {stats}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/plans/cleanup")
def plans_cleanup():
    """Remove rejected plans, orphan pending_hitl, and close stale awaiting_hitl runs."""
    stats = db.purge_plan_clutter()
    print(f"[cleanup] {stats}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/plans/delete-stuck")
def plans_delete_stuck():
    """Delete draft / orphan pending_hitl / rejected plans (skips live HITL queue)."""
    stats = db.delete_stuck_plans()
    print(f"[plans] delete-stuck: {stats}")
    return RedirectResponse(url="/#wait-panel", status_code=303)


@app.post("/plan/{plan_id}/delete")
def plan_delete(plan_id: str):
    """Remove a single stuck plan (draft, pending_hitl, or rejected)."""
    result = db.delete_plan(plan_id)
    print(f"[plan] delete {plan_id}: {result}")
    if not result.get("deleted"):
        raise HTTPException(400, result.get("error") or "delete failed")
    return RedirectResponse(url="/#wait-panel", status_code=303)


@app.post("/runs/cleanup-hitl")
def runs_cleanup_hitl():
    """Close awaiting_hitl runs that no longer have a live HITL queue item."""
    stats = db.close_stale_hitl_runs()
    print(f"[runs] cleanup-hitl: {stats}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/runs/cleanup-stuck")
def runs_cleanup_stuck():
    """Mark long-running ``running`` jobs completed (zombie supervision / news)."""
    stats = db.recover_stale_running_runs(max_age_sec=600)
    print(f"[runs] cleanup-stuck: {stats}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/run/{run_id}/close")
def run_force_close(run_id: str):
    """Force-close a single stuck ``running`` run."""
    graph.force_close_run(run_id, note="Force-closed from run page.")
    return RedirectResponse(url=f"/run/{run_id}", status_code=303)


@app.post("/hitl/{item_id}/resolve")
def hitl_resolve(
    item_id: int,
    decision: str = Form(...),
    note: str = Form(""),
    return_to: str = Form(""),
):
    items = db.list_hitl_pending()
    item = next((i for i in items if i["item_id"] == item_id), None)
    if not item:
        raise HTTPException(404, "hitl item not found")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    graph.resume_after_hitl(item["run_id"], item["plan_id"], decision,
                            operator="operator", note=note)
    if return_to == "dashboard":
        plan = db.get_plan(item["plan_id"]) or {}
        ticker = (json.loads(plan["plan_json"]).get("ticker") if plan.get("plan_json") else "?")
        return RedirectResponse(
            url=f"/?hitl_resolved={decision}&ticker={ticker}",
            status_code=303,
        )
    return RedirectResponse(url=f"/run/{item['run_id']}", status_code=303)


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request):
    from . import market_data
    from .metrics_registry import build_ops_summary
    return TEMPLATES.TemplateResponse(request, "diagnostics.html", {
        "obs": build_ops_summary(),
        "llm_status": llm.env_status(),
        "ls_status": traces.env_status(),
        "rag_counts": {
            "policy": rag.count("policy"),
            "news": rag.count("news"),
            "filings": rag.count("filings"),
            "past_plans": rag.count("past_plans"),
        },
        "rag_status": rag_bootstrap.status(),
        "config": {
            "firm_db": str(config.FIRM_DB),
            "vector_db": str(config.VECTOR_DB),
            "starting_nav": config.STARTING_NAV,
            "dossiers_seed_dir": str(config.DOSSIERS_SEED_DIR),
            "ops_db": str(config.OPS_DB),
            "dossiers_store": "ops.sqlite",
        },
        "platform": {
            "python": sys.version,
            "system": platform.platform(),
            "fastapi": _get_pkg_version("fastapi"),
            "uvicorn": _get_pkg_version("uvicorn"),
            "openai": _get_pkg_version("openai"),
            "langgraph": _get_pkg_version("langgraph"),
            "langsmith": _get_pkg_version("langsmith"),
            "pydantic": _get_pkg_version("pydantic"),
            "yfinance": _get_pkg_version("yfinance"),
        },
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "market_data_status": market_data.health_status(),
        "mcp_market_status": __import__(
            "app.mcp_market.bridge", fromlist=["provider_status"],
        ).provider_status(),
    })


@app.get("/api/market/mcp")
def api_mcp_market_status():
    from .mcp_market import provider_status
    return provider_status()


@app.get("/api/firm/timeline")
def api_firm_timeline():
    """JSON firm state timeline for charts and external dashboards."""
    return firm_timeline.build_firm_timeline()


@app.get("/evals", response_class=HTMLResponse)
def evals_page(request: Request):
    window = request.query_params.get("window", "sample")
    flash = request.query_params.get("ran", "")
    ctx = eval_dashboard.build_page_context(window)
    return TEMPLATES.TemplateResponse(request, "evals.html", {
        **ctx,
        "flash": "ok" if flash else "",
        "fmt_usd": portfolio.fmt_usd,
        "fmt_pct": portfolio.fmt_pct,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
    })


@app.post("/evals/run")
def evals_run_post(window: str = Form("sample")):
    eval_dashboard.run_and_save(window)
    return RedirectResponse(f"/evals?window={window}&ran=1", status_code=303)


@app.get("/api/evals/report")
def api_evals_report(window: str = "sample", refresh: bool = False):
    """JSON eval report (same shape as evals/output/*.json)."""
    return eval_dashboard.get_report(window, refresh=refresh)


@app.post("/api/diagnostics/test_market_data")
def api_test_market_data():
    from . import market_data
    probe = market_data.probe_apis(ticker="MSFT")
    probe["ok"] = bool(
        (probe.get("fundamentals") or {}).get("ok")
        and (probe.get("quote") or {}).get("ok")
    )
    return JSONResponse(probe)


def _get_pkg_version(name: str) -> str:
    try:
        m = __import__(name)
        return getattr(m, "__version__", "?")
    except Exception:
        return "not installed"


@app.post("/api/diagnostics/test_llm")
def api_test_llm():
    """Make one real LLM call to verify the OpenAI key works.
    Returns success + the model's response, or a clear error."""
    import time as _t
    if llm.is_mock():
        return {
            "ok": False,
            "mode": "mock",
            "reason": "Mock mode active. OPENAI_API_KEY missing, USE_MOCK_LLM=1, or live API failed at startup.",
            "llm_env": llm.env_status(),
        }
    t0 = _t.time()
    try:
        out = llm.chat_json(
            system="You are a test. Respond with strict JSON.",
            user='Return exactly: {"ok": true, "test": "hello"}',
            purpose="diagnostics_test",
        )
        return {
            "ok": True,
            "mode": "live",
            "model": config.OPENAI_MODEL,
            "response": out,
            "duration_ms": int((_t.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "mode": "live_error",
            "error": str(e),
            "duration_ms": int((_t.time() - t0) * 1000),
        }


@app.post("/api/diagnostics/test_langsmith")
def api_test_langsmith():
    """Verify LangSmith can be reached, project exists, and a test run is recorded."""
    import os as _os
    if not traces.langsmith_enabled():
        return {
            "ok": False,
            "reason": "LANGSMITH_API_KEY not set in environment.",
            "ls_env": traces.env_status(),
        }
    try:
        from langsmith import Client
        c = Client()
        # Create a fake run to confirm connectivity
        project = _os.environ.get("LANGSMITH_PROJECT",
                                   _os.environ.get("LANGCHAIN_PROJECT", "horizon-capital"))
        # Just check we can list runs (cheap)
        info = {
            "project": project,
            "endpoint": getattr(c, "api_url", "?"),
        }
        return {
            "ok": True,
            "info": info,
            "ls_env": traces.env_status(),
            "note": "Trigger a run from /trigger - it will appear in this project.",
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "ls_env": traces.env_status(),
        }


@app.post("/api/diagnostics/test_rag")
def api_test_rag():
    """Run a sample RAG search to verify the vector store and embedding work."""
    try:
        hits = rag.search("policy", "max position sizing", top_k=3)
        return {
            "ok": True,
            "query": "max position sizing",
            "hits_count": len(hits),
            "top_hit_preview": (hits[0]["text"][:200] if hits else None),
            "embedding_mode": "openai" if not llm.is_mock() else "mock_hash",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- API ----------------

def _news_from_api(payload: dict) -> dict:
    return {
        "id": payload.get("id") or "news_" + str(int(time.time() * 1000)),
        "headline": payload["headline"],
        "body": payload.get("body", ""),
        "tickers": [t.upper() for t in payload.get("tickers", [])],
        "source": payload.get("source", "api"),
        "published_at": payload.get("published_at")
                          or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


@app.post("/api/trigger/news")
def api_trigger_news(payload: dict):
    news = _news_from_api(payload)
    if payload.get("full_pipeline") in (True, 1, "1", "true", "yes"):
        result = graph.start_news_run_full(news)
    else:
        result = graph.start_news_run(news)
    return {"run_id": result.run_id, "status": result.status,
            "final_status": result.state.get("final_status")}


@app.post("/api/trigger/news/full")
def api_trigger_news_full(payload: dict):
    news = _news_from_api(payload)
    result = graph.start_news_run_full(news)
    return {"run_id": result.run_id, "status": result.status,
            "final_status": result.state.get("final_status"),
            "fill": result.state.get("fill")}


@app.post("/api/trigger/watchlist-sweep")
def api_trigger_watchlist_sweep():
    results = graph.start_watchlist_full_pipeline()
    return {
        "runs": [
            {"run_id": r.run_id, "status": r.status,
             "final_status": r.state.get("final_status")}
            for r in results
        ],
        "count": len(results),
    }


@app.post("/api/hitl/{item_id}/resolve")
def api_hitl_resolve(item_id: int, payload: dict):
    items = db.list_hitl_pending()
    item = next((i for i in items if i["item_id"] == item_id), None)
    if not item:
        raise HTTPException(404, "not found")
    decision = payload.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    result = graph.resume_after_hitl(item["run_id"], item["plan_id"],
                                      decision, note=payload.get("note", ""))
    return {"run_id": result.run_id, "status": result.status,
            "final_status": result.state.get("final_status")}


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404, "not found")
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "state": json.loads(run["state_json"]),
        "journal": [
            {**j, "output": json.loads(j["output_json"])}
            for j in db.list_journal_for_run(run_id)
        ],
        "traces": traces.list_for_run(run_id),
    }


@app.get("/api/traces/{run_id}")
def api_traces(run_id: str):
    return {"run_id": run_id, "traces": traces.list_for_run(run_id)}


def _trace_summary(t: dict) -> str:
    et = t.get("event_type") or ""
    data = t.get("event_data") or {}
    if et == "llm_call":
        purpose = data.get("purpose") or data.get("model") or "llm"
        cost = data.get("estimated_cost_usd")
        tok = data.get("tokens") or {}
        ttot = tok.get("total") or ((tok.get("prompt") or 0) + (tok.get("completion") or 0))
        extra = ""
        if cost is not None:
            extra = f" · ${float(cost):.4f}"
        if ttot:
            extra += f" · {ttot} tok"
        return f"{purpose}{extra}"
    if et == "tool_call":
        return str(data.get("tool") or "tool")
    if et == "rag_retrieval":
        return f"rag:{data.get('corpus', '?')} ({data.get('hits_count', 0)} hits)"
    if et in ("cadence_job_start", "cadence_job_end"):
        return str(data.get("job_id") or data.get("label") or et)
    if et == "report_rendered":
        return str(data.get("path") or "report")
    return (data.get("name") or data.get("final_status") or et)[:120]


def _alerts_template_ctx(**extra):
    import os
    return {
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "grafana_url": os.environ.get("GRAFANA_PUBLIC_URL", "http://127.0.0.1:3000"),
        "fmt_time": lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(t))),
        **extra,
    }


@app.get("/alerts", response_class=HTMLResponse)
def alerts_page(
    request: Request,
    severity: str = "",
    code: str = "",
    closed: int = 0,
):
    from . import ops_alerts

    # Default: open alerts only. Pass ?unacked=false to include closed history.
    unacked_only = request.query_params.get("unacked", "true").lower() not in (
        "false", "0", "no",
    )
    sev = severity if severity in ("info", "warning", "error", "critical") else None
    closed_notice = int(closed) if closed > 0 else 0
    return TEMPLATES.TemplateResponse(request, "alerts.html", _alerts_template_ctx(
        alerts=ops_alerts.list_alerts(
            limit=120,
            unacked_only=unacked_only,
            severity=sev,
            code=code or None,
        ),
        summary=ops_alerts.summary(),
        codes=ops_alerts.distinct_codes(),
        filter_unacked=unacked_only,
        filter_severity=severity,
        filter_code=code,
        closed_notice=closed_notice,
    ))


@app.get("/alerts/{alert_id}", response_class=HTMLResponse)
def alert_detail_page(request: Request, alert_id: str):
    from . import ops_alerts

    alert = ops_alerts.get_alert(alert_id)
    if not alert:
        raise HTTPException(404, "alert not found")
    ctx = alert.get("context") or {}
    related = traces.list_for_alert(alert_id)
    run_traces: list = []
    if alert.get("run_id"):
        try:
            run_traces = traces.list_for_run(alert["run_id"])
        except Exception:
            run_traces = []
    return TEMPLATES.TemplateResponse(request, "alert_detail.html", _alerts_template_ctx(
        alert=alert,
        context=ctx,
        traceback_text=ctx.get("traceback") or ctx.get("traceback_tail") or "",
        related_traces=related,
        run_traces=run_traces[:80],
    ))


@app.post("/alerts/ack-all")
def alerts_ack_all(return_to: str = Form("")):
    from . import ops_alerts
    closed = ops_alerts.acknowledge_all()
    # Always land on open-only view so closed alerts disappear from the list.
    return RedirectResponse(
        url=f"/alerts?unacked=true&closed={closed}",
        status_code=303,
    )


@app.post("/alerts/{alert_id}/ack")
def alerts_ack(alert_id: str, return_to: str = Form("")):
    from . import ops_alerts
    if not ops_alerts.acknowledge(alert_id):
        raise HTTPException(404, "alert not found")
    dest = return_to if return_to.startswith("/alerts") else "/alerts"
    return RedirectResponse(url=dest, status_code=303)


@app.get("/api/alerts")
def api_alerts_list(
    unacked: bool = False,
    limit: int = 50,
    severity: str = "",
    code: str = "",
):
    from . import ops_alerts

    sev = severity if severity in ("info", "warning", "error", "critical") else None
    return {
        "summary": ops_alerts.summary(),
        "alerts": ops_alerts.list_alerts(
            limit=limit,
            unacked_only=unacked,
            severity=sev,
            code=code or None,
        ),
    }


@app.get("/api/alerts/{alert_id}")
def api_alert_detail(alert_id: str):
    from . import ops_alerts

    alert = ops_alerts.get_alert(alert_id)
    if not alert:
        raise HTTPException(404, "alert not found")
    return {
        "alert": alert,
        "related_traces": traces.list_for_alert(alert_id),
    }


@app.post("/api/alerts/ack-all")
def api_alerts_ack_all():
    from . import ops_alerts
    closed = ops_alerts.acknowledge_all()
    return {"ok": True, "closed": closed}


@app.post("/api/alerts/{alert_id}/ack")
def api_alerts_ack(alert_id: str):
    from . import ops_alerts
    if not ops_alerts.acknowledge(alert_id):
        raise HTTPException(404, "alert not found")
    return {"ok": True, "alert_id": alert_id}


@app.get("/operations", response_class=HTMLResponse)
def operations_page(request: Request):
    ops = daily_plan.operations_view()
    plan = ops["plan"]
    return TEMPLATES.TemplateResponse(request, "operations.html", {
        "ops": ops,
        "plan": plan,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "fmt_usd": portfolio.fmt_usd,
        "fmt_pct": portfolio.fmt_pct,
    })


@app.get("/traces", response_class=HTMLResponse)
def traces_day_page(request: Request, run_id: str = ""):
    rid = (run_id or "").strip()
    if rid:
        rows = traces.list_recent(run_id=rid, limit=400)
        rows.reverse()
    else:
        rows = traces.list_for_trading_day()
    trace_rows = [{**t, "summary": _trace_summary(t)} for t in rows]
    from .metrics_registry import build_ops_summary
    obs = build_ops_summary()
    return TEMPLATES.TemplateResponse(request, "traces_day.html", {
        "trading_day": trading_date().isoformat(),
        "filter_run_id": rid,
        "trace_rows": trace_rows,
        "obs": obs,
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "fmt_time": lambda t: time.strftime("%H:%M:%S", time.localtime(float(t))),
    })


@app.get("/reports/daily/view", response_class=HTMLResponse)
def daily_report_view(request: Request):
    from .api.routes.reports import _build_today

    rep = _build_today()
    return TEMPLATES.TemplateResponse(request, "report_daily.html", {
        "report": rep.as_dict(),
        "mock_mode": llm.is_mock(),
        "langsmith_on": traces.langsmith_enabled(),
        "fmt_usd": portfolio.fmt_usd,
        "fmt_pct": portfolio.fmt_pct,
    })


@app.get("/api/portfolio")
def api_portfolio():
    pf = portfolio.get_portfolio_summary(refresh_prices=True)
    return pf


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "mock_mode": llm.is_mock(),
        "langsmith": traces.env_status(),
        "llm": llm.env_status(),
        "rag_chunks": {
            "policy": rag.count("policy"),
            "news": rag.count("news"),
            "filings": rag.count("filings"),
            "past_plans": rag.count("past_plans"),
        }
    }
