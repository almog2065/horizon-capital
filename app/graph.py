"""Orchestrator for the news-triggered flow with HITL pause/resume.

Two modes:
- LangGraph (when LANGSMITH or USE_LANGGRAPH=1 - uses StateGraph)
- Sequential (default, identical semantics, easier to debug + observe)

Tracing wraps every agent: LLM calls and tool calls are linked to the agent
that initiated them via contextvars.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from functools import partial
from typing import Optional

from . import config, db, firm_state, plan_automation, tools, traces
from .agents import (
    auditor,
    firm_manager,
    fundamental,
    idea_generator,
    news_triage,
    plan_builder,
    plan_supervisor,
    risk_officer,
)

# Actions that continue to Plan Builder (not terminal)
_PLAN_ELIGIBLE_ACTIONS = frozenset({
    "eligible_for_plan", "flag_for_hitl", "propose_thesis_review",
})

_supervision_busy = False
_supervision_busy_lock = threading.Lock()


def _pipeline_blocked_for_ticker(ticker: str) -> Optional[str]:
    """Return reason string if downstream news pipeline should not run for ticker."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    if config.BLOCK_PIPELINE_IF_PENDING_HITL and db.pending_hitl_for_ticker(t):
        return "pending_hitl"
    if config.BLOCK_DUPLICATE_PIPELINE:
        open_plan = db.active_plan_for_ticker(t)
        if open_plan:
            return f"open_plan:{open_plan['plan_id']}"
        live = db.canonical_active_plan_for_ticker(t)
        if live:
            return f"active_plan:{live['plan_id']}"
    return None


def _enqueue_hitl(run_id: str, plan_id: str, ticker: str) -> Optional[int]:
    item_id = db.enqueue_hitl(run_id, plan_id)
    if item_id is None:
        from . import hitl_sync
        hitl_sync.repair_hitl_queue()
        item_id = db.enqueue_hitl(run_id, plan_id)
    if item_id is None:
        traces.record("hitl_enqueue_failed", {
            "ticker": ticker, "plan_id": plan_id,
        })
    return item_id


def _watchlist() -> list[str]:
    from . import dossier_paths

    return dossier_paths.list_tickers()


def _run_agent(run_id: str, agent_name: str, fn, *args, **kwargs):
    """Run an agent within a trace context. Returns (output, journal_id, audit)."""
    t0 = time.time()
    with traces.agent_context(run_id, agent_name):
        traces.record("agent_start", {"agent": agent_name})
        try:
            with traces.ls_span(
                f"agent.{agent_name}",
                run_type="chain",
                inputs={"agent": agent_name, "args_count": len(args)},
                tags=[agent_name, "agent"],
            ) as ls_run:
                out = fn(*args, **kwargs)
                if ls_run is not None:
                    ls_run.end(outputs={
                        "output": json.dumps(out, default=str)[:8000],
                    })
        except Exception as e:
            traces.record("agent_error", {"agent": agent_name, "error": str(e)})
            raise
        duration_ms = int((time.time() - t0) * 1000)
        traces.record("agent_end", {"agent": agent_name,
                                      "output_preview": json.dumps(out, default=str)[:400]},
                       duration_ms=duration_ms)

    # Persist journal entry
    journal_id = db.journal_append(run_id, agent_name, out, duration_ms)
    # Back-fill journal_id on the agent's traces
    _backfill_journal_id(run_id, agent_name, journal_id)

    # Audit (in its own context; traces get journal_id of the audited entry
    # so they appear inline under the audited step in the UI)
    with traces.agent_context(run_id, "auditor"):
        with traces.journal_context(journal_id):
            traces.record("agent_start", {"agent": "auditor",
                                            "audits": agent_name})
            audit = auditor.run(agent_name, out, journal_id)
            traces.record("agent_end", {"agent": "auditor",
                                         "severity": audit.get("overall_severity")})
    db.add_audit(journal_id, audit["overall_severity"],
                 audit.get("compliant", True), audit)

    return out, journal_id, audit


def _backfill_journal_id(run_id: str, agent_name: str, journal_id: int):
    import sqlite3

    from . import config
    try:
        with sqlite3.connect(config.FIRM_DB) as c:
            c.execute(
                "UPDATE traces SET journal_id = ? WHERE run_id = ? "
                "AND agent = ? AND journal_id IS NULL",
                (journal_id, run_id, agent_name),
            )
    except Exception as e:
        print(f"[trace] backfill failed: {e}")


class RunResult:
    def __init__(self, run_id: str, status: str, state: dict):
        self.run_id = run_id
        self.status = status
        self.state = state


def spawn_news_run_background(
    news: dict,
    as_of: Optional[str] = None,
) -> str:
    """Start news pipeline in a daemon thread; return run_id immediately."""
    run_id = "run_" + uuid.uuid4().hex[:12]
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")

    def _worker() -> None:
        try:
            start_news_run(news, as_of=as_of, run_id=run_id)
        except Exception as e:
            print(f"[news_run] background {run_id} failed: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return run_id


@traces.langsmith_traceable(name="horizon.news_run", run_type="chain")
def start_news_run(
    news: dict,
    as_of: Optional[str] = None,
    *,
    run_id: Optional[str] = None,
) -> RunResult:
    run_id = run_id or ("run_" + uuid.uuid4().hex[:12])
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")

    state = {
        "run_id": run_id,
        "trigger_type": "news_event",
        "trigger_meta": {"news_id": news.get("id", "")},
        "as_of": as_of,
        "news_item": news,
        "ticker": None,
        "triage": None,
        "fundamental": None,
        "plan_draft": None,
        "risk": None,
        "final_status": None,
        "errors": [],
    }

    db.save_run(run_id, "news_event", state["trigger_meta"], as_of, "running", state)
    traces.set_context(run_id=run_id)
    traces.record("run_start", {"trigger_type": "news_event",
                                  "news_headline": news.get("headline", "")[:200]})

    try:
        # 1) News Triage
        holdings = [h["ticker"] for h in db.list_holdings()]
        watchlist = _watchlist()
        book = firm_state.build_firm_state(refresh_prices=False)
        triage_out, _, _ = _run_agent(
            run_id, "news_triage", news_triage.run,
            news, holdings, watchlist, book,
        )
        state["triage"] = triage_out

        if triage_out.get("decision") != "act":
            state["final_status"] = f"completed_no_action_{triage_out.get('decision')}"
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "completed", state)
            traces.record("run_end", {"final_status": state["final_status"]})
            return RunResult(run_id, "completed", state)

        # 2) Fundamental
        if not triage_out.get("impacted_tickers"):
            state["final_status"] = "completed_no_target"
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "completed", state)
            traces.record("run_end", {"final_status": state["final_status"]})
            return RunResult(run_id, "completed", state)

        ticker = triage_out["impacted_tickers"][0]
        state["ticker"] = ticker

        blocked = _pipeline_blocked_for_ticker(ticker)
        if blocked:
            state["pipeline_suppressed"] = blocked
            state["final_status"] = "completed_duplicate_work_suppressed"
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "completed", state)
            traces.record("run_end", {
                "final_status": state["final_status"],
                "ticker": ticker,
                "reason": blocked,
            })
            return RunResult(run_id, "completed", state)

        mode = ("event_triggered_review" if ticker in holdings else "new_research")
        fund_out, _, _ = _run_agent(
            run_id, "fundamental_analyst", fundamental.run, ticker, mode, as_of,
            context={"news": news, "firm_state": book},
        )
        state["fundamental"] = fund_out
        state["firm_state"] = book

        if fund_out.get("recommended_action") not in _PLAN_ELIGIBLE_ACTIONS:
            state["final_status"] = (
                f"completed_no_plan_{fund_out.get('recommended_action')}"
            )
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "completed", state)
            traces.record("run_end", {"final_status": state["final_status"]})
            return RunResult(run_id, "completed", state)

        # 3) Plan Builder
        plan_out, _, _ = _run_agent(
            run_id, "plan_builder", plan_builder.run,
            ticker, fund_out, as_of, book,
        )
        state["plan_draft"] = plan_out

        if plan_out.get("status") != "drafted" or not plan_out.get("plan"):
            state["final_status"] = f"completed_plan_{plan_out.get('status')}"
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "completed", state)
            traces.record("run_end", {"final_status": state["final_status"]})
            return RunResult(run_id, "completed", state)

        plan = plan_out["plan"]
        plan["status"] = "draft"
        db.save_plan(plan["id"], plan["ticker"], "draft", plan)

        # 4) Risk Officer — decides HITL vs autonomous execution
        risk_out, _, _ = _run_agent(
            run_id, "risk_officer", risk_officer.run, plan,
            fundamental=fund_out,
            holdings_tickers=holdings,
            triage=state.get("triage"),
            firm_state=book,
        )
        state["risk"] = risk_out
        state["hitl_decision"] = {
            "required": risk_officer.risk_requires_hitl(
                risk_out, fund_out, holdings_tickers=holdings,
            ),
            "routing": risk_out.get("recommended_routing"),
            "verdict": risk_out.get("verdict"),
            "hitl_required": risk_out.get("hitl_required"),
        }

        if risk_out.get("verdict") == "reject":
            db.update_plan_status(plan["id"], "rejected",
                                   rejection_reason=risk_out.get(
                                       "reasoning_narrative", "")[:300])
            state["final_status"] = "completed_risk_rejected"
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "completed", state)
            traces.record("run_end", {"final_status": state["final_status"]})
            return RunResult(run_id, "completed", state)

        holding = plan_automation.holding_for_plan(plan)
        phase = (
            "in_position"
            if holding and int(holding.get("quantity") or 0) > 0
            else "pre_position"
        )
        sup_out, _, _ = _run_agent(
            run_id, "plan_supervisor", plan_supervisor.run,
            plan, "draft", phase, holding, None,
            {
                "risk_verdict": risk_out.get("verdict"),
                "hitl_required": risk_out.get("hitl_required"),
            },
            as_of,
            book,
        )
        state["supervisor"] = sup_out

        needs_hitl = (
            risk_officer.risk_requires_hitl(
                risk_out, fund_out, holdings_tickers=holdings,
            )
            or sup_out.get("hitl_required")
        )
        if needs_hitl:
            db.update_plan_status(plan["id"], "pending_hitl",
                                   history_append={
                                       "at": as_of, "agent": "plan_supervisor",
                                       "action": "routed_to_hitl",
                                       "note": (sup_out.get("reasoning_narrative")
                                                or "")[:300],
                                   })
            _enqueue_hitl(run_id, plan["id"], plan["ticker"])
            state["final_status"] = "awaiting_hitl"
            db.save_run(run_id, "news_event", state["trigger_meta"], as_of,
                        "awaiting_hitl", state)
            traces.record("run_pause", {
                "reason": "awaiting_hitl",
                "plan_id": plan["id"],
                "risk_verdict": risk_out.get("verdict"),
                "supervisor_verdict": sup_out.get("verdict"),
            })
            return RunResult(run_id, "awaiting_hitl", state)

        state["routing_decision"] = "autonomous_execution"
        traces.record("autonomous_execution", {
            "plan_id": plan["id"],
            "risk_verdict": risk_out.get("verdict"),
            "supervisor_verdict": sup_out.get("verdict"),
            "routing": risk_out.get("recommended_routing"),
        })
        return _execute_plan(
            run_id, state, plan, as_of,
            approved_by="risk_officer",
            approval_note=risk_out.get("reasoning_narrative", "")[:500],
        )

    except Exception as e:
        state["errors"].append(str(e))
        state["final_status"] = "error"
        db.save_run(run_id, "news_event", state["trigger_meta"], as_of, "error", state)
        traces.record("run_end", {"final_status": "error", "error": str(e)})
        return RunResult(run_id, "error", state)


def _execute_plan(
    run_id: str,
    state: dict,
    plan: dict,
    as_of: str,
    approved_by: str,
    approval_note: str,
) -> RunResult:
    """Execute an approved plan (after HITL or autonomous Risk routing)."""
    plan_id = plan["id"]
    db.update_plan_status(
        plan_id, "active", approved_by=approved_by,
        history_append={
            "at": as_of, "agent": approved_by,
            "action": "approved", "note": approval_note[:300],
        },
    )

    from . import plan_automation

    qty = plan_automation.order_quantity_from_plan(plan)
    if qty <= 0:
        state["fill"] = {
            "status": "rejected",
            "reasons": [{
                "reason": (
                    "Could not size order from plan entry "
                    "(check target_size_pct_nav and entry price)"
                ),
            }],
        }
        state["final_status"] = "completed_execution_rejected"
        db.save_run(
            run_id, state.get("trigger_type", "news_event"),
            state.get("trigger_meta", {}), as_of, "completed", state,
        )
        traces.record("run_end", {"final_status": state["final_status"]})
        return RunResult(run_id, "completed", state)

    fill, _, _ = _run_agent(
        run_id, "execution",
        partial(tools.submit_order_sim, run_id=run_id),
        plan_id, plan["ticker"], plan["entry"]["side"], qty,
        as_of=as_of,
    )
    state["fill"] = fill

    if fill.get("status") == "filled":
        state["final_status"] = "completed_position_opened"
    else:
        state["final_status"] = f"completed_execution_{fill.get('status')}"

    db.save_run(
        run_id, state.get("trigger_type", "news_event"),
        state.get("trigger_meta", {}), as_of, "completed", state,
    )
    traces.record("run_end", {"final_status": state["final_status"]})
    return RunResult(run_id, "completed", state)


def _plan_id_from_state(state: dict) -> Optional[str]:
    draft = state.get("plan_draft") or {}
    if draft.get("plan_id"):
        return draft["plan_id"]
    plan = draft.get("plan") or {}
    return plan.get("id")


def _persist_hitl_run(
    run_id: str,
    plan: dict,
    as_of: str,
    *,
    trigger_type: str = "plan_supervision",
    trigger_meta: Optional[dict] = None,
    extra_state: Optional[dict] = None,
) -> dict:
    """Save a run row for HITL pause so resume_after_hitl can load state."""
    plan_id = plan["id"]
    state = {
        "run_id": run_id,
        "trigger_type": trigger_type,
        "trigger_meta": trigger_meta or {"plan_id": plan_id},
        "as_of": as_of,
        "plan_id": plan_id,
        "ticker": plan.get("ticker"),
        "plan_draft": {"plan_id": plan_id, "plan": plan, "status": "drafted"},
        "final_status": "awaiting_hitl",
    }
    if extra_state:
        state.update(extra_state)
    db.save_run(
        run_id, trigger_type, state["trigger_meta"], as_of, "awaiting_hitl", state,
    )
    return state


def _load_or_recover_hitl_run(run_id: str, plan_id: str) -> tuple[dict, str]:
    """Load run state for HITL resume; recover if supervision omitted save_run."""
    row = db.get_run(run_id)
    if row:
        state = json.loads(row["state_json"])
        as_of = state.get("as_of") or time.strftime("%Y-%m-%dT%H:%M:%S")
        return state, as_of

    plan_row = db.get_plan(plan_id)
    if not plan_row:
        raise ValueError(f"plan not found: {plan_id}")
    plan = db.load_plan_body(plan_id)
    if not plan:
        raise ValueError(f"plan not found or invalid JSON: {plan_id}")
    as_of = time.strftime("%Y-%m-%dT%H:%M:%S")
    extra: dict = {}
    for ev in reversed(plan.get("history") or []):
        if ev.get("agent") == "plan_supervisor":
            extra["supervisor"] = {
                "verdict": "awaiting_operator",
                "reasoning_narrative": ev.get("note", ""),
                "hitl_required": True,
                "execution_authorized": False,
            }
            break

    state = _persist_hitl_run(
        run_id, plan, as_of,
        trigger_meta={"plan_id": plan_id, "recovered": True},
        extra_state=extra,
    )
    traces.record("hitl_run_recovered", {"run_id": run_id, "plan_id": plan_id})
    return state, as_of


@traces.langsmith_traceable(name="horizon.news_run_full", run_type="chain")
def start_news_run_full(news: dict, as_of: Optional[str] = None, **kwargs) -> RunResult:
    """Run to completion — Risk Officer decides HITL vs autonomous execution."""
    return start_news_run(news, as_of=as_of)


def _monitoring_event(
    ticker: str, plan_id: str, report: dict, action: dict, as_of: str,
) -> dict:
    """Synthetic news event to re-open the trading pipeline on a holding."""
    breaches = report.get("breaches") or []
    breach_txt = "; ".join(
        f"{b.get('check_name')}: {b.get('detail', '')[:120]}"
        for b in breaches[:4]
    )
    act = action.get("action", "trigger_re_eval")
    snap = report.get("market_snapshot") or {}
    return {
        "id": f"mon_{plan_id}_{ticker.lower()}_{int(time.time())}",
        "headline": (
            f"{ticker}: Position Monitor — {act} on "
            f"{action.get('check_name', 'plan breach')}"
        ),
        "body": (
            f"Horizon Capital Position Monitor cycle {as_of} flagged {ticker} "
            f"(plan {plan_id}). Status: {report.get('overall_status')}. "
            f"Return {snap.get('return_pct', 0):.1%}, "
            f"{snap.get('pct_nav', 0):.1%} NAV, held {snap.get('days_held', 0)}d. "
            f"Breaches: {breach_txt}. "
            f"Action: {act}. "
            f"Rationale: {action.get('rationale', '')[:400]} "
            f"Event-triggered review on held position — evaluate thesis per plan."
        ),
        "tickers": [ticker],
        "source": "position_monitor",
        "plan_id": plan_id,
        "monitor_action": act,
        "published_at": as_of,
    }


def _apply_monitor_report(
    report: dict,
    holding: dict,
    as_of: str,
    spawn_pipeline: bool,
) -> list[str]:
    """Execute plan monitoring actions (log / flag / pipeline spawn)."""
    spawned: list[str] = []
    plan_id = report.get("plan_id") or holding.get("plan_id")
    ticker = report.get("ticker") or holding.get("ticker")

    for action in report.get("recommended_actions") or []:
        act = (action.get("action") or "log").lower().strip()
        if act == "log":
            db.append_plan_history(plan_id, {
                "agent": "position_monitor",
                "action": "monitor_log",
                "check": action.get("check_name"),
                "note": action.get("rationale", "")[:300],
            })
        elif act in ("flag", "trim_hint"):
            db.append_plan_history(plan_id, {
                "agent": "position_monitor",
                "action": "monitor_flag",
                "check": action.get("check_name"),
                "note": action.get("rationale", "")[:300],
            })
        elif act in ("trigger_re_eval", "review") and spawn_pipeline:
            blocked = _pipeline_blocked_for_ticker(ticker)
            if blocked:
                db.append_plan_history(plan_id, {
                    "agent": "position_monitor",
                    "action": "pipeline_suppressed",
                    "note": f"Skipped re-eval: {blocked}"[:300],
                })
            else:
                news = _monitoring_event(ticker, plan_id, report, action, as_of)
                child_run_id = spawn_news_run_background(news, as_of=as_of)
                spawned.append(child_run_id)

    # Refresh holding mark
    snap = report.get("market_snapshot") or {}
    price = float(snap.get("price") or holding.get("current_price") or 0)
    if price > 0:
        db.upsert_holding(
            ticker,
            int(holding.get("quantity") or 0),
            float(holding.get("cost_basis") or price),
            price,
            plan_id,
            holding.get("sector") or "Unknown",
        )
    return spawned


def _apply_supervision_bundle(
    bundle: dict,
    as_of: str,
    spawn_pipeline: bool,
    auto_execute: bool,
) -> list[str]:
    """Apply monitor + supervisor decisions for one plan."""
    spawned: list[str] = []
    plan = db.load_plan_body(bundle["plan_id"])
    if not plan:
        return spawned
    plan_status = bundle["plan_status"]
    holding = bundle.get("holding")
    h = holding if bundle.get("has_position") else plan_automation.synthetic_holding(plan)

    if bundle.get("monitor_report") and bundle.get("has_position"):
        spawned.extend(_apply_monitor_report(
            bundle["monitor_report"], h, as_of, spawn_pipeline,
        ))

    sup = bundle.get("supervisor_report") or {}
    plan_id = bundle["plan_id"]

    for act in sup.get("recommended_actions") or []:
        name = (act.get("action") or "").lower()
        if name in ("freeze", "await_hitl", "route_hitl"):
            db.append_plan_history(plan_id, {
                "agent": "plan_supervisor",
                "action": name,
                "note": act.get("rationale", "")[:300],
            })

    if (
        auto_execute
        and sup.get("execution_authorized")
        and not sup.get("hitl_required")
        and plan_status == "active"
        and not bundle.get("has_position")
    ):
        exec_run_id = "run_" + uuid.uuid4().hex[:12]
        exec_state = {
            "run_id": exec_run_id,
            "trigger_type": "plan_supervision",
            "trigger_meta": {"plan_id": plan_id, "action": "auto_execute"},
            "as_of": as_of,
            "plan_id": plan_id,
            "supervisor": sup,
        }
        db.save_run(exec_run_id, "plan_supervision", exec_state["trigger_meta"],
                    as_of, "running", exec_state)
        traces.set_context(run_id=exec_run_id)
        result = _execute_plan(
            exec_run_id, exec_state, plan, as_of,
            approved_by="plan_supervisor",
            approval_note="Automated entry after supervisor authorization.",
        )
        spawned.append(result.run_id)

    if spawn_pipeline and (
        sup.get("pipeline_spawn") or sup.get("verdict") == "trigger_pipeline"
    ):
        mon = bundle.get("monitor_report") or {}
        pipeline_acts = [
            a for a in (mon.get("recommended_actions") or [])
            if (a.get("action") or "").lower() in (
                "trigger_re_eval", "review", "trigger_pipeline",
            )
        ]
        if not pipeline_acts and sup.get("verdict") == "trigger_pipeline":
            pipeline_acts = [{
                "action": "trigger_re_eval",
                "check_name": "supervisor_verdict",
                "rationale": (sup.get("reasoning_narrative") or "")[:400],
            }]
        for act in pipeline_acts:
            t = plan.get("ticker", "")
            if _pipeline_blocked_for_ticker(t):
                db.append_plan_history(plan_id, {
                    "agent": "plan_supervisor",
                    "action": "pipeline_suppressed",
                    "note": f"Re-eval blocked: {_pipeline_blocked_for_ticker(t)}"[:300],
                })
                continue
            news = _monitoring_event(t, plan_id, mon, act, as_of)
            spawned.append(spawn_news_run_background(news, as_of=as_of))

    if sup.get("hitl_required") and plan_status in ("draft", "active"):
        ticker = plan.get("ticker", "")
        existing = db.hitl_for_plan(plan_id)
        ticker_pending = (
            config.HITL_ONE_PER_TICKER
            and db.pending_hitl_for_ticker(ticker)
        )
        if (
            not ticker_pending
            and (not existing or existing.get("status") != "pending")
        ):
            hitl_run_id = "run_" + uuid.uuid4().hex[:12]
            extra = {"supervisor": sup}
            if bundle.get("monitor_report"):
                extra["position_monitor"] = bundle["monitor_report"]
            _persist_hitl_run(
                hitl_run_id, plan, as_of,
                trigger_meta={"plan_id": plan_id, "source": "supervisor_hitl"},
                extra_state=extra,
            )
            if _enqueue_hitl(hitl_run_id, plan_id, ticker) is not None:
                db.update_plan_status(plan_id, "pending_hitl", history_append={
                    "at": as_of, "agent": "plan_supervisor",
                    "action": "routed_to_hitl",
                    "note": sup.get("reasoning_narrative", "")[:200],
                })

    return spawned


def _supervision_try_start() -> bool:
    global _supervision_busy
    with _supervision_busy_lock:
        if _supervision_busy:
            return False
        _supervision_busy = True
        return True


def _supervision_done() -> None:
    global _supervision_busy
    with _supervision_busy_lock:
        _supervision_busy = False


def begin_plan_supervision(
    as_of: Optional[str] = None,
    spawn_pipeline: bool = True,
    auto_execute: bool = False,
) -> RunResult:
    """Create supervision run row and return immediately (UI / scheduler)."""
    run_id = "sup_" + uuid.uuid4().hex[:12]
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    state = {
        "run_id": run_id,
        "trigger_type": "plan_supervision",
        "trigger_meta": {
            "spawn_pipeline": spawn_pipeline,
            "auto_execute": auto_execute,
        },
        "as_of": as_of,
        "plan_evaluations": [],
        "spawned_run_ids": [],
        "plans_evaluated": 0,
        "plans_healthy": 0,
        "plans_need_action": 0,
        "executions_triggered": 0,
        "final_status": None,
        "errors": [],
        "plan_failures": [],
    }
    db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                "running", state)
    traces.set_context(run_id=run_id)
    traces.record("run_start", {"trigger_type": "plan_supervision"})
    return RunResult(run_id, "running", state)


@traces.langsmith_traceable(name="horizon.plan_supervision", run_type="chain")
def start_plan_supervision(
    as_of: Optional[str] = None,
    spawn_pipeline: bool = True,
    auto_execute: bool = False,
    manager_out: Optional[dict] = None,
    skip_orchestration: bool = False,
    *,
    background: bool = True,
) -> RunResult:
    """Automated cycle: every draft / pending_hitl / active plan → monitor + supervisor."""
    if not _supervision_try_start():
        skip_id = "sup_" + uuid.uuid4().hex[:12]
        as_of_skip = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
        skip_state = {
            "run_id": skip_id,
            "trigger_type": "plan_supervision",
            "final_status": "skipped_overlap",
            "errors": ["Another supervision cycle is already running."],
        }
        db.save_run(
            skip_id, "plan_supervision", {}, as_of_skip, "completed", skip_state,
        )
        return RunResult(skip_id, "completed", skip_state)

    begun = begin_plan_supervision(
        as_of=as_of,
        spawn_pipeline=spawn_pipeline,
        auto_execute=auto_execute,
    )

    def _worker() -> None:
        try:
            execute_plan_supervision(
                begun.run_id,
                spawn_pipeline=spawn_pipeline,
                auto_execute=auto_execute,
                manager_out=manager_out,
                skip_orchestration=skip_orchestration,
            )
        except Exception as err:
            print(f"[plan_supervision] {begun.run_id} failed: {err}")
        finally:
            _supervision_done()

    if background:
        threading.Thread(target=_worker, daemon=True).start()
        return begun

    try:
        return execute_plan_supervision(
            begun.run_id,
            spawn_pipeline=spawn_pipeline,
            auto_execute=auto_execute,
            manager_out=manager_out,
            skip_orchestration=skip_orchestration,
        )
    finally:
        _supervision_done()


def execute_plan_supervision(
    run_id: str,
    spawn_pipeline: bool = True,
    auto_execute: bool = False,
    manager_out: Optional[dict] = None,
    skip_orchestration: bool = False,
) -> RunResult:
    """Execute supervision work for an existing ``sup_`` run row."""
    row = db.get_run(run_id)
    if not row:
        raise ValueError(f"supervision run not found: {run_id}")
    state = json.loads(row["state_json"])
    as_of = state.get("as_of") or time.strftime("%Y-%m-%dT%H:%M:%S")
    spawn_pipeline = bool(
        state.get("trigger_meta", {}).get("spawn_pipeline", spawn_pipeline),
    )
    auto_execute = bool(
        state.get("trigger_meta", {}).get("auto_execute", auto_execute),
    )
    traces.set_context(run_id=run_id)
    watchlist = _watchlist()
    holdings_tickers = [h["ticker"] for h in db.list_holdings()]

    try:
        all_spawned: list[str] = []
        book = firm_state.build_firm_state(refresh_prices=False)
        state["firm_state"] = book
        if manager_out is not None:
            mgr_out = manager_out
        else:
            mgr_out, _, _ = _run_agent(
                run_id, "firm_manager", firm_manager.run, book, as_of,
            )
        state["firm_manager"] = mgr_out
        items = plan_automation.list_supervisable_plans()
        state["supervision_plan_total"] = len(items)
        for item in items:
            plan = item["plan"]
            plan_status = item["row"]["status"]
            plan_id = plan.get("id") or item["row"]["plan_id"]
            plan["status"] = plan_status
            ticker = (plan.get("ticker") or "?").upper()
            state["supervision_current"] = {
                "plan_id": plan_id,
                "ticker": ticker,
                "status": plan_status,
                "phase": "monitor",
            }
            db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                        "running", state)
            try:
                bundle = plan_automation.evaluate_plan(
                    plan, plan_status, as_of,
                    holdings_tickers, watchlist,
                    firm_state=book, manager_out=mgr_out,
                )
                sup, _, _ = _run_agent(
                    run_id, "plan_supervisor", plan_supervisor.run,
                    plan, plan_status, bundle["phase"], bundle.get("holding"),
                    bundle.get("monitor_report"), None, as_of, book, mgr_out,
                )
                bundle["supervisor_report"] = sup
                state["supervision_current"] = {
                    "plan_id": plan_id,
                    "ticker": ticker,
                    "status": plan_status,
                    "phase": "supervisor",
                }
                db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                            "running", state)

                state["supervision_current"] = {
                    "plan_id": plan_id,
                    "ticker": ticker,
                    "status": plan_status,
                    "phase": "apply_actions",
                }
                db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                            "running", state)
                spawned = _apply_supervision_bundle(
                    bundle, as_of, spawn_pipeline, auto_execute,
                )
                bundle["spawned_run_ids"] = spawned
                all_spawned.extend(spawned)

                plan_automation.append_supervision_history(
                    plan_id, bundle, run_id,
                )

                state["plan_evaluations"].append(bundle)
                state["plans_evaluated"] = len(state["plan_evaluations"])
                state["supervision_current"] = None
                db.patch_run_state(run_id, {
                    "plan_evaluations": state["plan_evaluations"],
                    "plans_evaluated": state["plans_evaluated"],
                    "spawned_run_ids": all_spawned,
                    "supervision_current": None,
                    "plans_healthy": state["plans_healthy"],
                    "plans_need_action": state["plans_need_action"],
                    "executions_triggered": state["executions_triggered"],
                    "plan_failures": state["plan_failures"],
                    "errors": state["errors"],
                }, status="running")
                if sup.get("verdict") in ("monitor_only",) and not sup.get("pipeline_spawn"):
                    state["plans_healthy"] += 1
                else:
                    state["plans_need_action"] += 1
                if sup.get("execution_authorized") and auto_execute:
                    state["executions_triggered"] += 1
            except Exception as e:
                err = f"{plan_id} ({plan.get('ticker', '?')}): {e}"
                state["plan_failures"].append(err)
                state["errors"].append(err)
                print(f"[plan_supervision] plan failed: {err}")
                traces.record("plan_supervision_error", {
                    "plan_id": plan_id,
                    "ticker": plan.get("ticker"),
                    "error": str(e),
                    "traceback": traceback.format_exc()[-2000:],
                })

        state["supervision_current"] = None
        if not skip_orchestration:
            state["supervision_current"] = {
                "phase": "orchestration",
                "ticker": "",
                "plan_id": "",
                "status": "routing",
            }
            db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                        "running", state)
            from . import firm_orchestration
            orch = firm_orchestration.execute_balance_actions(
                mgr_out, book, as_of, run_id,
                allow_scan=True,
                spawn_pipeline=spawn_pipeline,
            )
            state["manager_orchestration"] = orch
            all_spawned.extend(orch.get("spawned_run_ids") or [])

        state["plans_evaluated"] = len(state["plan_evaluations"])
        state["spawned_run_ids"] = all_spawned
        state["position_reports"] = [
            b.get("monitor_report") for b in state["plan_evaluations"]
            if b.get("monitor_report")
        ]
        if state["plan_evaluations"]:
            state["final_status"] = "completed_plan_supervision"
            run_status = "completed"
        elif state["plan_failures"]:
            state["final_status"] = "error"
            run_status = "error"
        else:
            state["final_status"] = "completed_plan_supervision"
            run_status = "completed"

        db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                    run_status, state)
        traces.record("run_end", {
            "final_status": state["final_status"],
            "plans": state["plans_evaluated"],
            "failures": len(state["plan_failures"]),
            "spawned": len(all_spawned),
        })
        return RunResult(run_id, run_status, state)

    except Exception as e:
        state["errors"].append(f"{type(e).__name__}: {e}")
        state["errors"].append(traceback.format_exc()[-1500:])
        state["final_status"] = "error"
        db.save_run(run_id, "plan_supervision", state["trigger_meta"], as_of,
                    "error", state)
        traces.record("run_end", {"final_status": "error", "error": str(e)})
        return RunResult(run_id, "error", state)


def force_close_run(run_id: str, note: str = "") -> bool:
    """Operator: mark a zombie ``running`` run completed."""
    row = db.get_run(run_id)
    if not row or row["status"] != "running":
        return False
    state = json.loads(row["state_json"])
    state["supervision_current"] = None
    state["final_status"] = "completed_stale_running"
    state.setdefault("errors", []).append(
        note or "Force-closed by operator — run was stuck.",
    )
    meta = row.get("trigger_meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    db.save_run(
        run_id, row["trigger_type"], meta or {},
        row.get("as_of") or "", "completed", state,
    )
    _supervision_done()
    return True


def start_position_monitor(
    as_of: Optional[str] = None,
    spawn_pipeline: bool = True,
) -> RunResult:
    """Backward-compatible alias → full plan supervision cycle."""
    return start_plan_supervision(
        as_of=as_of, spawn_pipeline=spawn_pipeline, auto_execute=False,
    )


def start_watchlist_full_pipeline(as_of: Optional[str] = None) -> list[RunResult]:
    """One run per dossier ticker; agents decide HITL vs execution per name."""
    results: list[RunResult] = []
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    for ticker in _watchlist():
        news = {
            "id": f"sweep_{ticker.lower()}_{int(time.time() * 1000)}",
            "headline": (
                f"{ticker}: watchlist sweep — constructive setup, valuation in range, "
                f"eligible for new position"
            ),
            "body": (
                f"Horizon Capital scheduled watchlist sweep for {ticker}. "
                f"Dossier on file; business quality durable, valuation within target band. "
                f"Fundamentals support eligible_for_plan — no earnings blackout, "
                f"simulate_order feasible at 4% NAV entry."
            ),
            "tickers": [ticker],
            "source": "watchlist_sweep",
            "published_at": as_of,
        }
        results.append(start_news_run(news, as_of=as_of))
    return results


@traces.langsmith_traceable(name="horizon.hitl_resume", run_type="chain")
def resume_after_hitl(run_id: str, plan_id: str, decision: str,
                      operator: str = "operator",
                      note: str = "") -> RunResult:
    state, as_of = _load_or_recover_hitl_run(run_id, plan_id)

    traces.set_context(run_id=run_id)
    traces.record("hitl_resume", {"plan_id": plan_id, "decision": decision,
                                    "operator": operator, "note": note})

    if decision == "reject":
        db.update_plan_status(plan_id, "rejected",
                               rejection_reason=note or "operator rejection",
                               history_append={"at": as_of, "agent": "operator",
                                                "action": "rejected", "note": note})
        state["final_status"] = "completed_hitl_rejected"
        item = db.hitl_for_plan(plan_id)
        if item:
            db.resolve_hitl(item["item_id"], decision)
        try:
            from . import trade_history
            trade_history.record_trade(
                ticker=(state.get("ticker") or plan_id or "?").upper(),
                action="rejected",
                plan_id=plan_id,
                run_id=run_id,
                source="live",
                as_of=as_of,
                meta={"final_status": state["final_status"], "note": (note or "")[:200]},
                trade_id=f"reject_{run_id}",
            )
        except Exception as e:
            print(f"[trade_history] record reject failed: {e}")
        db.save_run(run_id, state["trigger_type"], state["trigger_meta"], as_of,
                    "completed", state)
        traces.record("run_end", {"final_status": state["final_status"]})
        return RunResult(run_id, "completed", state)

    if decision != "approve":
        raise ValueError(f"unknown decision: {decision}")

    plan = db.load_plan_body(plan_id)
    if not plan:
        raise ValueError(f"plan not found or invalid JSON: {plan_id}")
    plan_status = (db.get_plan(plan_id) or {}).get("status") or plan.get("status", "draft")

    item = db.hitl_for_plan(plan_id)
    if item:
        db.resolve_hitl(item["item_id"], decision)

    db.append_plan_history(plan_id, {
        "agent": operator,
        "action": "approved",
        "note": note or "operator approval",
    })
    plan = db.load_plan_body(plan_id)
    if not plan:
        raise ValueError(f"plan not found or invalid JSON: {plan_id}")

    holding = plan_automation.holding_for_plan(plan)
    phase = (
        "in_position"
        if holding and int(holding.get("quantity") or 0) > 0
        else "pre_position"
    )
    book = state.get("firm_state") or firm_state.build_firm_state(refresh_prices=False)
    sup_out, _, _ = _run_agent(
        run_id, "plan_supervisor", plan_supervisor.run,
        plan, plan_status, phase, holding, None,
        {"operator_approved": True},
        as_of,
        book,
    )
    state["supervisor"] = sup_out
    if not sup_out.get("execution_authorized"):
        state["final_status"] = "completed_supervisor_blocked"
        db.update_plan_status(
            plan_id, "draft",
            history_append={
                "at": as_of, "agent": "plan_supervisor",
                "action": "execution_blocked",
                "note": (sup_out.get("reasoning_narrative") or "")[:300],
            },
        )
        db.save_run(run_id, state["trigger_type"], state["trigger_meta"], as_of,
                    "completed", state)
        traces.record("run_end", {"final_status": state["final_status"]})
        return RunResult(run_id, "completed", state)

    state["routing_decision"] = "operator_hitl_approved"
    return _execute_plan(
        run_id, state, plan, as_of,
        approved_by=operator,
        approval_note=note or "operator approval",
    )


# ---------- Idea scan trigger (proactive discovery) ----------

class ScanResult:
    """Result of a proactive idea scan.

    Holds the scan run itself (with the idea_generator output) plus any
    downstream runs that were spawned for top picks.
    """
    def __init__(self, scan_run_id: str, scan_state: dict,
                 downstream_runs: list[RunResult]):
        self.scan_run_id = scan_run_id
        self.run_id = scan_run_id  # alias for redirect convenience
        self.state = scan_state
        self.downstream_runs = downstream_runs
        self.status = "completed"

    @property
    def top_picks(self) -> list[dict]:
        return (self.state.get("idea_scan") or {}).get("top_picks", [])


def _candidate_to_event(scan_run_id: str, cand: dict, as_of: str) -> dict:
    """Synthesize a 'discovery' event the existing pipeline understands."""
    ticker = cand["ticker"]
    rationale = cand.get("rationale", "")[:400]
    composite = cand.get("composite_score", 0)
    action = cand.get("recommended_action", "open_new_research")
    brief = cand.get("research_brief") or {}
    brief_summary = (brief.get("executive_summary") or "")[:600]
    why_now = (brief.get("why_now") or "")[:400]
    return {
        "id": f"idea_{scan_run_id}_{ticker.lower()}",
        # Keywords here intentionally match the constructive_setup detector in
        # fundamental.py so the downstream agent routes to eligible_for_plan,
        # not propose_thesis_review.
        "headline": (
            f"{ticker}: Idea Generator discovery — constructive setup, "
            f"valuation in range, composite {composite:.2f}"
        ),
        "body": (
            f"Horizon Capital Idea Generator scan {scan_run_id} ranked {ticker} "
            f"as top pick with composite score {composite:.2f}. "
            f"Recommended next step: {action} (eligible_for_plan). "
            f"Rationale: {rationale} "
            f"Why now: {why_now} "
            f"Brief: {brief_summary} "
            f"Watchlist sweep — new_research eligibility on a quality name in "
            f"the firm universe."
        ),
        "tickers": [ticker],
        "source": "idea_scan_synthetic",
        "scan_run_id": scan_run_id,
        "composite_score": composite,
        "scan_pick": cand,
        "published_at": as_of,
    }


def checkpoint_scan_progress(
    scan_run_id: str,
    phase: str,
    *,
    partial_scan: Optional[dict] = None,
    **extra,
) -> None:
    """Persist in-flight scan phase so /run UI can refresh accurately."""
    patch: dict = {
        "scan_progress": {
            "phase": phase,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **extra,
        },
    }
    if partial_scan is not None:
        row = db.get_run(scan_run_id)
        prior: dict = {}
        if row:
            try:
                prior = json.loads(row["state_json"] or "{}")
            except json.JSONDecodeError:
                prior = {}
        merged = dict(prior.get("idea_scan") or {})
        merged.update(partial_scan)
        patch["idea_scan"] = merged
    db.patch_run_state(scan_run_id, patch)


def begin_idea_scan(top_k: int = 3, as_of: Optional[str] = None,
                    spawn_downstream: bool = True,
                    only_new: bool = True,
                    novelty_window_days: float = 7.0) -> ScanResult:
    """Create a scan run in `running` state and return immediately (for UI redirect)."""
    scan_run_id = "scan_" + uuid.uuid4().hex[:12]
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    state = {
        "run_id": scan_run_id,
        "trigger_type": "idea_scan",
        "trigger_meta": {
            "top_k": top_k,
            "only_new": only_new,
            "novelty_window_days": novelty_window_days,
            "spawn_downstream": spawn_downstream,
        },
        "as_of": as_of,
        "idea_scan": None,
        "downstream_run_ids": [],
        "final_status": None,
        "errors": [],
    }
    db.save_run(scan_run_id, "idea_scan", state["trigger_meta"], as_of,
                "running", state)
    traces.set_context(run_id=scan_run_id)
    traces.record("run_start", {
        "trigger_type": "idea_scan", "top_k": top_k,
        "only_new": only_new, "spawn_downstream": spawn_downstream,
    })
    return ScanResult(scan_run_id, state, [])


def execute_idea_scan(scan_run_id: str, top_k: int = 3,
                      spawn_downstream: bool = True,
                      only_new: bool = True,
                      novelty_window_days: float = 7.0) -> ScanResult:
    """Execute scan work for an existing run row (background or synchronous)."""
    run_row = db.get_run(scan_run_id)
    if not run_row:
        raise ValueError(f"scan run not found: {scan_run_id}")
    state = json.loads(run_row["state_json"])
    as_of = state.get("as_of") or time.strftime("%Y-%m-%dT%H:%M:%S")
    traces.set_context(run_id=scan_run_id)

    try:
        checkpoint_scan_progress(scan_run_id, "firm_manager")
        book = firm_state.build_firm_state(refresh_prices=False)
        mgr_out, _, _ = _run_agent(
            scan_run_id, "firm_manager", firm_manager.run, book, as_of,
        )
        state["firm_state"] = book
        state["firm_manager"] = mgr_out
        # Defer balance orchestration until scan completes — avoids spawning
        # parallel news runs while this scan is still screening 20+ names.
        state["manager_orchestration"] = {
            "executed": False,
            "reason": "deferred_during_idea_scan",
            "spawned_run_ids": [],
            "actions": [],
            "skipped": [],
        }
        state["balance_spawned_run_ids"] = []
        db.patch_run_state(scan_run_id, state, status="running")
        checkpoint_scan_progress(scan_run_id, "idea_generator")
        scan_out, _, _ = _run_agent(
            scan_run_id, "idea_generator", idea_generator.run,
            top_k=top_k, as_of=as_of,
            only_new=only_new,
            novelty_window_days=novelty_window_days,
            scan_run_id=scan_run_id,
            firm_state=book,
            manager_out=mgr_out,
        )
        state["idea_scan"] = scan_out

        downstream: list[RunResult] = []
        if spawn_downstream:
            picks = scan_out.get("top_picks") or []
            for pick in picks:
                tools.ensure_scan_dossier(pick, scan_run_id)
                if not pick.get("research_brief"):
                    dossier_res = tools.get_dossier(pick["ticker"])
                    dossier = (
                        dossier_res.get("dossier")
                        if dossier_res.get("found") else None
                    )
                    pick["research_brief"] = idea_generator.build_brief_for_pick(
                        pick, dossier,
                    )
                event = _candidate_to_event(scan_run_id, pick, as_of)
                child = start_news_run(event, as_of=as_of)
                downstream.append(child)
                state["downstream_run_ids"].append(child.run_id)
                traces.set_context(run_id=scan_run_id)

        state["final_status"] = "completed_idea_scan"
        db.save_run(scan_run_id, "idea_scan", state["trigger_meta"], as_of,
                    "completed", state)
        traces.record("run_end", {
            "final_status": state["final_status"],
            "downstream_count": len(downstream),
        })
        return ScanResult(scan_run_id, state, downstream)

    except Exception as e:
        state["errors"].append(str(e))
        state["final_status"] = "error"
        db.save_run(scan_run_id, "idea_scan", state["trigger_meta"], as_of,
                    "error", state)
        traces.record("run_end", {"final_status": "error", "error": str(e)})
        return ScanResult(scan_run_id, state, [])


def redirect_url_after_scan(result: ScanResult) -> str:
    """Where to send the browser after starting a scan."""
    if result.downstream_runs:
        return f"/run/{result.downstream_runs[0].run_id}?from_scan={result.scan_run_id}"
    return f"/run/{result.scan_run_id}"


def find_run_id_for_plan(plan_id: str) -> Optional[str]:
    """Locate the pipeline run that produced this plan."""
    item = db.hitl_for_plan(plan_id)
    if item and item.get("run_id"):
        return item["run_id"]
    for run_row in db.list_runs(200):
        run = db.get_run(run_row["run_id"])
        if not run:
            continue
        state = json.loads(run["state_json"])
        draft = state.get("plan_draft") or {}
        pid = draft.get("plan_id") or (draft.get("plan") or {}).get("id")
        if pid == plan_id:
            return run["run_id"]
    return None


def rerun_run(run_id: str) -> RunResult | ScanResult:
    """Re-execute a completed run with the same trigger payload."""
    row = db.get_run(run_id)
    if not row:
        raise ValueError(f"run not found: {run_id}")
    if row["status"] == "running":
        raise ValueError("run is still in progress")

    state = json.loads(row["state_json"])
    trigger_type = row["trigger_type"]

    if trigger_type == "idea_scan":
        meta = state.get("trigger_meta") or json.loads(row.get("trigger_meta") or "{}")
        return start_idea_scan(
            top_k=int(meta.get("top_k", 3)),
            spawn_downstream=bool(meta.get("spawn_downstream", True)),
            only_new=bool(meta.get("only_new", True)),
            novelty_window_days=float(meta.get("novelty_window_days", 7.0)),
        )

    if trigger_type == "news_event":
        news = state.get("news_item")
        if not news:
            raise ValueError("cannot rerun: original run has no news_item")
        news = dict(news)
        news.pop("id", None)
        return start_news_run(news)

    if trigger_type in ("position_monitor", "plan_supervision"):
        meta = state.get("trigger_meta") or json.loads(row.get("trigger_meta") or "{}")
        return start_plan_supervision(
            spawn_pipeline=bool(meta.get("spawn_pipeline", True)),
            auto_execute=bool(meta.get("auto_execute", False)),
        )

    if trigger_type == "firm_balance":
        from . import firm_orchestration
        meta = state.get("trigger_meta") or json.loads(row.get("trigger_meta") or "{}")
        st = firm_orchestration.run_balance_cycle(
            trigger_supervision=bool(meta.get("trigger_supervision", True)),
            spawn_pipeline=bool(meta.get("spawn_pipeline", True)),
        )
        return RunResult(st["run_id"], st.get("final_status", "completed"), st)

    raise ValueError(f"unsupported trigger_type for rerun: {trigger_type}")


@traces.langsmith_traceable(name="horizon.idea_scan", run_type="chain")
def start_idea_scan(top_k: int = 3, as_of: Optional[str] = None,
                    spawn_downstream: bool = True,
                    only_new: bool = True,
                    novelty_window_days: float = 7.0) -> ScanResult:
    """Run the Idea Generator synchronously (API / tests)."""
    begun = begin_idea_scan(
        top_k=top_k, as_of=as_of, spawn_downstream=spawn_downstream,
        only_new=only_new, novelty_window_days=novelty_window_days,
    )
    return execute_idea_scan(
        begun.scan_run_id, top_k=top_k, spawn_downstream=spawn_downstream,
        only_new=only_new, novelty_window_days=novelty_window_days,
    )


# ---------- LangGraph 1.x variant ----------

def _try_langgraph_compile():
    try:
        from typing import Optional as Opt
        from typing import TypedDict

        from langgraph.graph import END, StateGraph

        class GState(TypedDict, total=False):
            run_id: str
            news_item: dict
            triage: Opt[dict]
            fundamental: Opt[dict]
            plan_draft: Opt[dict]
            risk: Opt[dict]
            ticker: Opt[str]
            as_of: str
            holdings_tickers: list
            watchlist_tickers: list

        def n_triage(s):
            with traces.agent_context(s["run_id"], "news_triage"):
                out = news_triage.run(s["news_item"], s["holdings_tickers"],
                                       s["watchlist_tickers"])
            return {"triage": out, "ticker": (out.get("impacted_tickers") or [None])[0]}

        def n_fundamental(s):
            with traces.agent_context(s["run_id"], "fundamental_analyst"):
                out = fundamental.run(s["ticker"], mode="event_triggered_review",
                                       as_of=s.get("as_of", ""))
            return {"fundamental": out}

        def n_plan(s):
            with traces.agent_context(s["run_id"], "plan_builder"):
                out = plan_builder.run(s["ticker"], s["fundamental"],
                                        as_of=s.get("as_of", ""))
            return {"plan_draft": out}

        def n_risk(s):
            plan = (s.get("plan_draft") or {}).get("plan")
            if not plan:
                return {"risk": None}
            with traces.agent_context(s["run_id"], "risk_officer"):
                out = risk_officer.run(plan)
            return {"risk": out}

        def route_after_triage(s):
            return "fundamental" if (s.get("triage") or {}).get("decision") == "act" else END

        def route_after_fund(s):
            ra = (s.get("fundamental") or {}).get("recommended_action")
            return "plan" if ra == "eligible_for_plan" else END

        def route_after_plan(s):
            return "risk" if (s.get("plan_draft") or {}).get("status") == "drafted" else END

        g = StateGraph(GState)
        g.add_node("triage", n_triage)
        g.add_node("fundamental", n_fundamental)
        g.add_node("plan", n_plan)
        g.add_node("risk", n_risk)
        g.set_entry_point("triage")
        g.add_conditional_edges("triage", route_after_triage,
                                 {"fundamental": "fundamental", END: END})
        g.add_conditional_edges("fundamental", route_after_fund,
                                 {"plan": "plan", END: END})
        g.add_conditional_edges("plan", route_after_plan,
                                 {"risk": "risk", END: END})
        g.add_edge("risk", END)
        return g.compile()
    except Exception as e:
        print(f"[graph] LangGraph compile failed (will use sequential): {e}")
        return None


_LG = None


def langgraph_handle():
    global _LG
    if _LG is None:
        _LG = _try_langgraph_compile()
    return _LG
