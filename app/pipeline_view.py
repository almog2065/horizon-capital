"""Build a pipeline-progress description for rendering a UI status bar.

Each function returns a `Pipeline` dict:
{
  "title": "News run pipeline",
  "stages": [
    {"key": "news_triage", "label": "News Triage", "state": "done",
     "meta": "act · 0.88", "tooltip": "..."},
    ...
  ],
  "summary": "4 of 6 stages complete"
}

Stage states:
  - "done"     → finished successfully (green)
  - "running"  → currently in progress (yellow, pulsing)
  - "pending"  → waiting (gray)
  - "skipped"  → bypassed by routing (light gray, dashed)
  - "error"    → failed (red)

The view layer (Jinja partial) renders these dicts as horizontal pills with
connector lines between them. Pure data → pure CSS, no JS required.
"""
from __future__ import annotations

import time
from typing import Optional

from . import db, traces

_DISCOVERY_TOOLS = frozenset({
    "discover_idea_candidates", "discover_recent_8k", "fetch_company_profile",
})

_NEWS_AGENT_STAGE = {
    "news_triage": "news_triage",
    "fundamental_analyst": "fundamental_analyst",
    "plan_builder": "plan_builder",
    "risk_officer": "risk_officer",
    "auditor": "risk_officer",
    "operator": "hitl",
    "execution": "execution",
}


_NEWS_STAGES = [
    ("news_triage",         "News Triage"),
    ("fundamental_analyst", "Fundamental"),
    ("plan_builder",        "Plan Builder"),
    ("risk_officer",        "Risk Officer"),
    ("hitl",                "HITL"),
    ("execution",           "Execution"),
]

_SCAN_STAGES = [
    ("scan_init",           "Scan Init"),
    ("api_discovery",       "API Discovery"),
    ("screening",           "Screening"),
    ("ranking",             "Ranking"),
    ("briefs",              "Research Briefs"),
    ("downstream",          "Downstream Runs"),
]

def _attach_news_tools(stages: dict[str, dict], tools_by_agent: dict) -> None:
    for agent, tool_list in tools_by_agent.items():
        key = _NEWS_AGENT_STAGE.get(agent)
        if not key or key not in stages:
            continue
        existing = stages[key].get("tools") or []
        merged = list(existing)
        for t in tool_list:
            if t not in merged:
                merged.append(t)
        stages[key]["tools"] = merged


def _attach_scan_tools(stages: dict[str, dict], tools_by_agent: dict) -> None:
    ig = tools_by_agent.get("idea_generator", [])
    disc = [t for t in ig if t in _DISCOVERY_TOOLS]
    screen = [t for t in ig if t not in _DISCOVERY_TOOLS]
    if disc and "api_discovery" in stages:
        stages["api_discovery"]["tools"] = disc
    if screen:
        if "screening" in stages:
            stages["screening"]["tools"] = screen
        if "ranking" in stages:
            stages["ranking"]["tools"] = [
                t for t in screen
                if t.startswith(("search_", "get_", "simulate_"))
            ] or screen[:6]
        if "briefs" in stages:
            stages["briefs"]["tools"] = [
                t for t in screen if t in ("get_dossier", "fetch_fundamentals")
            ] or ["get_dossier"]


def _buy_signal_meta(state: dict, stage_key: str) -> str:
    """Short label when this stage produced a buy / plan signal."""
    fund = state.get("fundamental") or {}
    plan_draft = state.get("plan_draft") or {}
    risk = state.get("risk") or {}
    fill = state.get("fill") or {}
    if stage_key == "fundamental_analyst":
        a = fund.get("recommended_action", "")
        if a == "eligible_for_plan":
            return "→ BUY signal"
        if a in ("flag_for_hitl", "propose_thesis_review"):
            return "→ review for BUY"
    if stage_key == "plan_builder" and plan_draft.get("status") == "drafted":
        return "→ plan drafted"
    if stage_key == "risk_officer":
        v = risk.get("verdict", "")
        if v in ("approve", "approve_with_modification"):
            return "→ approved"
        if risk.get("hitl_required"):
            return "→ HITL for BUY"
    if stage_key == "execution" and fill.get("status") == "filled":
        return "→ FILLED"
    return ""


_PLAN_STAGES = [
    ("draft",               "Draft"),
    ("risk_review",         "Risk Review"),
    ("hitl",                "HITL"),
    ("active",              "Active"),
    ("closed",              "Closed"),
]

_MONITOR_AGENT_STAGE = {
    "firm_manager": "evaluate",
    "plan_supervisor": "evaluate",
    "position_monitor": "evaluate",
}

_BALANCE_AGENT_STAGE = {
    "firm_manager": "manager",
}

_SCAN_AGENT_STAGE = {
    "firm_manager": "scan_init",
    "idea_generator": "screening",
}

_SCAN_PROGRESS_STAGE = {
    "firm_manager": "scan_init",
    "discovery": "api_discovery",
    "idea_generator": "screening",
    "screening": "screening",
    "ranking": "ranking",
    "llm_ranking": "ranking",
    "briefs": "briefs",
}


def _infer_scan_stage_from_tools(tools_by_agent: dict) -> Optional[str]:
    ig = tools_by_agent.get("idea_generator", [])
    if "llm:idea_scan" in ig:
        return "ranking"
    if any(t.startswith("fetch_") or t == "simulate_order" for t in ig):
        return "screening"
    if any(t in _DISCOVERY_TOOLS for t in ig):
        return "api_discovery"
    if tools_by_agent.get("firm_manager"):
        return "scan_init"
    return None


def _apply_scan_progress_stages(
    stages: dict[str, dict],
    state: dict,
    run_status: str,
) -> None:
    """Use scan_progress + partial idea_scan while run is in flight."""
    if run_status != "running":
        return
    scan = state.get("idea_scan") or {}
    if scan.get("ranked_candidates"):
        return
    sp = state.get("scan_progress") or {}
    phase = sp.get("phase", "")
    active = _SCAN_PROGRESS_STAGE.get(phase)
    if phase == "screening":
        ev = int(sp.get("evaluated") or 0)
        tot = int(sp.get("screening_total") or 0)
        meta = f"{ev}/{tot} screened" if tot else "fundamentals"
        if active and stages.get(active):
            stages[active].update({"state": "running", "meta": meta})
        if stages.get("api_discovery", {}).get("state") != "skipped":
            stages["api_discovery"].update({"state": "done", "meta": "EDGAR + pool"})
        return
    if phase == "discovery" and stages.get("api_discovery"):
        stages["api_discovery"].update({"state": "running", "meta": "EDGAR + pool"})
        return
    if active and stages.get(active):
        meta = phase.replace("_", " ")
        if phase == "llm_ranking":
            meta = "LLM refine ranking"
        stages[active].update({"state": "running", "meta": meta})
    if scan.get("discovery") and stages.get("api_discovery"):
        stages["api_discovery"].update({"state": "done", "meta": "EDGAR + pool"})


def _journal_rows(run_id: Optional[str]) -> list[dict]:
    if not run_id:
        return []
    return db.list_journal_for_run(run_id)


def _mark_running_from_journal(
    stages: dict[str, dict],
    stage_order: list[tuple[str, str]],
    run_id: Optional[str],
    run_status: str,
    *,
    agent_to_stage: dict[str, str],
) -> Optional[str]:
    """Highlight the active pipeline step from journal + in-flight run status."""
    if run_status not in ("running", "awaiting_hitl") or not run_id:
        return None

    keys = [k for k, _ in stage_order]
    rows = _journal_rows(run_id)
    if not rows:
        if stages[keys[0]]["state"] == "pending":
            stages[keys[0]].update({
                "state": "running",
                "meta": "starting…",
                "tooltip": "No agent output yet — run just started.",
            })
            return keys[0]
        return None

    seen: list[str] = []
    last_agent = ""
    for row in rows:
        agent = row.get("agent") or ""
        last_agent = agent
        sk = agent_to_stage.get(agent)
        if sk and sk not in seen:
            seen.append(sk)

    for key in keys:
        if key in seen and stages[key]["state"] in ("pending", "running"):
            if stages[key]["state"] != "skipped":
                stages[key]["state"] = "done"
                if not stages[key].get("meta"):
                    stages[key]["meta"] = "completed"

    running_key: Optional[str] = None
    agent_label = last_agent.replace("_", " ") if last_agent else "agent"
    last_stage = agent_to_stage.get(last_agent)
    if last_stage and stages.get(last_stage, {}).get("state") not in (
            "done", "skipped", "error"):
        running_key = last_stage
        stages[last_stage].update({
            "state": "running",
            "meta": f"in progress · {agent_label}",
            "tooltip": f"Agent {last_agent} is active on {stages[last_stage]['label']}.",
        })
    else:
        last_done_idx = -1
        for i, key in enumerate(keys):
            if stages[key]["state"] == "done":
                last_done_idx = i
        for key in keys[last_done_idx + 1:]:
            if stages[key]["state"] in ("skipped", "error"):
                continue
            running_key = key
            stages[key].update({
                "state": "running",
                "meta": f"queued · after {agent_label}" if last_agent else "starting…",
                "tooltip": (
                    f"Next step: {stages[key]['label']}. "
                    f"Last completed agent: {last_agent or '—'}."
                ),
            })
            break

    if run_status == "awaiting_hitl" and "hitl" in stages:
        if stages["hitl"]["state"] not in ("skipped", "error", "done"):
            stages["hitl"].update({
                "state": "running",
                "meta": "awaiting operator",
                "tooltip": "Plan paused until operator approves or rejects.",
            })
            running_key = "hitl"

    return running_key


def run_progress_hint(
    run: dict,
    state: dict,
    pipeline: dict,
    *,
    journal: Optional[list[dict]] = None,
) -> dict:
    """Operator-facing hint: where the run is (or was) blocked."""
    now = time.time()
    created = float(run.get("created_at") or now)
    age_sec = max(0.0, now - created)
    status = run.get("status") or ""
    rows = journal if journal is not None else _journal_rows(run.get("run_id"))
    last_agent = (rows[-1].get("agent") if rows else "") or ""
    last_ts = float(rows[-1].get("ts") or created) if rows else created
    idle_sec = max(0.0, now - last_ts)

    current = None
    for s in pipeline.get("stages") or []:
        if s.get("state") == "running":
            current = s
            break

    waiting_on = ""
    if status == "awaiting_hitl":
        waiting_on = "Operator HITL approval"
    elif state.get("supervision_current"):
        sc = state["supervision_current"]
        waiting_on = f"Plan supervision · {sc.get('ticker', '?')} ({sc.get('phase', '')})"
    elif current:
        waiting_on = current.get("label") or current.get("key", "")
    elif status == "running":
        sp = state.get("scan_progress") or {}
        if sp.get("phase") == "screening" and sp.get("screening_total"):
            waiting_on = (
                f"Screening {sp.get('evaluated', 0)}/{sp['screening_total']} candidates"
            )
        elif sp.get("phase"):
            waiting_on = sp["phase"].replace("_", " ").title()
        elif last_agent:
            waiting_on = last_agent.replace("_", " ").title()

    # Scans can take several minutes (many market-data calls); higher idle threshold.
    scan_running = (
        status == "running"
        and (state.get("trigger_type") == "idea_scan")
        and not (state.get("idea_scan") or {}).get("ranked_candidates")
    )
    stuck_idle = 300 if scan_running else 90
    is_stuck = (
        status == "running"
        and age_sec >= 120
        and idle_sec >= stuck_idle
    )

    return {
        "age_sec": age_sec,
        "idle_sec": idle_sec,
        "last_agent": last_agent,
        "current_stage": current,
        "waiting_on": waiting_on,
        "is_stuck": is_stuck,
        "status": status,
    }


# ---------- news run ----------

def build_news_pipeline(state: dict, run_status: str,
                        run_id: Optional[str] = None) -> dict:
    """Pipeline for a news-triggered run.

    Reads booleans off the state dict and final_status to decide each stage.
    Handles all early-exit paths (off-universe → ignore, watch, no-plan, etc.)
    by marking subsequent stages as 'skipped' rather than 'pending'.
    """
    triage = state.get("triage") or {}
    fundamental = state.get("fundamental") or {}
    plan_draft = state.get("plan_draft") or {}
    risk = state.get("risk") or {}
    fill = state.get("fill") or {}
    final_status = state.get("final_status") or ""

    stages: dict[str, dict] = {k: {"key": k, "label": label,
                                      "state": "pending", "meta": "", "tooltip": ""}
                                  for k, label in _NEWS_STAGES}

    # News Triage
    if triage:
        decision = triage.get("decision", "")
        score = triage.get("materiality_score") or 0
        stages["news_triage"].update({
            "state": "done",
            "meta": f"{decision} · {float(score):.2f}",
            "tooltip": triage.get("reasoning", "")[:160],
        })
        # If triage said ignore/watch, downstream stages are skipped (not pending)
        if decision != "act":
            for k in ("fundamental_analyst", "plan_builder",
                       "risk_officer", "hitl", "execution"):
                stages[k]["state"] = "skipped"

    # Fundamental
    if fundamental:
        action = fundamental.get("recommended_action", "")
        strength = fundamental.get("thesis_strength", "")
        stages["fundamental_analyst"].update({
            "state": "done",
            "meta": f"{action} · {strength or '—'}",
            "tooltip": (fundamental.get("reasoning_narrative") or "")[:160],
        })
        if action not in ("eligible_for_plan", "flag_for_hitl",
                            "propose_thesis_review"):
            for k in ("plan_builder", "risk_officer", "hitl", "execution"):
                stages[k]["state"] = "skipped"

    # Plan Builder
    if plan_draft:
        pstatus = plan_draft.get("status", "")
        plan_id = (plan_draft.get("plan") or {}).get("id") or plan_draft.get("plan_id")
        stages["plan_builder"].update({
            "state": "done" if pstatus == "drafted" else "skipped",
            "meta": pstatus + (f" · {plan_id[:14]}" if plan_id else ""),
            "tooltip": (plan_draft.get("reasoning_narrative") or "")[:160],
        })
        if pstatus != "drafted":
            for k in ("risk_officer", "hitl", "execution"):
                stages[k]["state"] = "skipped"

    # Risk Officer
    if risk:
        verdict = risk.get("verdict", "")
        routing = risk.get("recommended_routing", "")
        state_val = "done"
        if verdict == "reject":
            state_val = "error"
        stages["risk_officer"].update({
            "state": state_val,
            "meta": f"{verdict} → {routing}",
            "tooltip": (risk.get("reasoning_narrative") or "")[:160],
        })
        if verdict == "reject":
            for k in ("hitl", "execution"):
                stages[k]["state"] = "skipped"

    # HITL
    if final_status == "awaiting_hitl":
        stages["hitl"].update({
            "state": "running",
            "meta": "awaiting operator",
            "tooltip": "Plan paused until operator approves or rejects.",
        })
    elif final_status == "completed_hitl_rejected":
        stages["hitl"].update({
            "state": "error",
            "meta": "operator rejected",
        })
        stages["execution"]["state"] = "skipped"
    elif final_status == "completed_position_opened":
        # Reached execution → HITL implicitly approved (operator or autonomous)
        if stages["hitl"]["state"] not in ("skipped", "error"):
            stages["hitl"].update({
                "state": "done",
                "meta": "approved",
            })
    elif risk and risk.get("verdict") == "approve" and risk.get(
            "recommended_routing") == "execution":
        # Autonomous path: HITL was bypassed by Risk
        if stages["hitl"]["state"] == "pending":
            stages["hitl"].update({
                "state": "skipped", "meta": "autonomous (Risk approved)",
            })

    # Execution
    if fill:
        s = fill.get("status", "")
        st = "done" if s == "filled" else "error"
        stages["execution"].update({
            "state": st,
            "meta": (
                f"{s} · {fill.get('quantity', 0)} @ "
                f"${fill.get('fill_price', 0):.2f}"
                if s == "filled" else s
            ),
            "tooltip": str(fill)[:160],
        })

    # Global override: errored run paints the currently-running stage red
    if run_status == "error":
        for k in ("hitl", "execution", "risk_officer", "plan_builder",
                   "fundamental_analyst", "news_triage"):
            if stages[k]["state"] == "running":
                stages[k]["state"] = "error"

    tools_by_agent = traces.tools_by_agent(run_id) if run_id else {}
    _attach_news_tools(stages, tools_by_agent)
    for key, _ in _NEWS_STAGES:
        buy = _buy_signal_meta(state, key)
        if buy:
            prev = stages[key].get("meta", "")
            stages[key]["meta"] = f"{prev} {buy}".strip() if prev else buy

    all_tools: list[str] = []
    for tl in tools_by_agent.values():
        for t in tl:
            if t not in all_tools:
                all_tools.append(t)
    active_key = _mark_running_from_journal(
        stages, _NEWS_STAGES, run_id, run_status, agent_to_stage=_NEWS_AGENT_STAGE,
    )
    stage_list = [stages[k] for k, _ in _NEWS_STAGES]
    done = sum(1 for s in stage_list if s["state"] == "done")
    total_relevant = sum(1 for s in stage_list if s["state"] != "skipped")
    summary = f"{done} of {total_relevant} stages complete"
    if active_key and run_status in ("running", "awaiting_hitl"):
        active = stages[active_key]
        summary = f"In progress: {active['label']}" + (
            f" — {active.get('meta', '')}" if active.get("meta") else ""
        )

    return {
        "title": "News run pipeline",
        "stages": stage_list,
        "summary": summary,
        "tools_used": all_tools,
        "active_stage_key": active_key,
    }


# ---------- scan run ----------

def build_scan_pipeline(state: dict, run_status: str,
                        run_id: Optional[str] = None) -> dict:
    """Pipeline for an idea-scan run.

    Uses state.idea_scan substructure and downstream_run_ids to decide
    stages. Each scan goes: init → API discovery → screening → ranking →
    briefs → downstream runs (if any).
    """
    scan = state.get("idea_scan") or {}
    downstream_ids = state.get("downstream_run_ids") or []
    state.get("final_status") or ""

    stages: dict[str, dict] = {k: {"key": k, "label": label,
                                      "state": "pending", "meta": "", "tooltip": ""}
                                  for k, label in _SCAN_STAGES}

    # Scan init — always done if state has trigger_type=idea_scan
    if state.get("trigger_type") == "idea_scan":
        stages["scan_init"].update({
            "state": "done",
            "meta": f"top_k={state.get('trigger_meta', {}).get('top_k', '?')}",
        })

    scan_incomplete = run_status == "running" and not scan.get("ranked_candidates")
    if scan_incomplete:
        spawn = state.get("trigger_meta", {}).get("spawn_downstream", True)
        _apply_scan_progress_stages(stages, state, run_status)
        if stages["api_discovery"]["state"] == "pending":
            stages["api_discovery"].update({
                "state": "running", "meta": "EDGAR + pool",
            })
        if stages["screening"]["state"] == "pending":
            stages["screening"].update({
                "state": "pending", "meta": "fundamentals",
            })
        if stages["ranking"]["state"] == "pending":
            stages["ranking"].update({"state": "pending", "meta": "rank"})
        if stages["briefs"]["state"] == "pending":
            stages["briefs"].update({"state": "pending", "meta": "briefs"})
        if spawn:
            stages["downstream"].update({
                "state": "pending", "meta": "after top picks",
            })
        else:
            stages["downstream"].update({
                "state": "skipped", "meta": "spawn disabled",
            })

    # API Discovery
    discovery = scan.get("discovery") or {}
    if discovery.get("enabled"):
        s = discovery.get("edgar_status")
        if s == "edgar":
            channels = discovery.get("edgar_channels") or ["8-K"]
            stages["api_discovery"].update({
                "state": "done",
                "meta": (
                    f"EDGAR {'+'.join(channels)} · "
                    f"{discovery.get('edgar_filings_count', 0)} filings · "
                    f"+{discovery.get('api_additions', 0)} new"
                ),
                "tooltip": (
                    f"New tickers: "
                    f"{', '.join(discovery.get('edgar_new_tickers', []))}"
                    + (
                        f" · {discovery.get('material_8k_count', 0)} material 8-K"
                        if discovery.get("material_8k_count") else ""
                    )
                ),
            })
        elif s == "fallback":
            stages["api_discovery"].update({
                "state": "error",
                "meta": "fallback to static pool",
                "tooltip": discovery.get("edgar_error", "")[:160],
            })
    elif discovery.get("enabled") is False:
        stages["api_discovery"].update({
            "state": "skipped",
            "meta": "disabled",
        })

    # Screening (evaluated tickers)
    if scan.get("candidates_evaluated") is not None:
        stages["screening"].update({
            "state": "done",
            "meta": (
                f"{scan.get('candidates_evaluated', 0)} evaluated · "
                f"{scan.get('candidates_passed_screen', 0)} passed"
            ),
        })

    # Ranking + top picks
    if scan.get("ranked_candidates"):
        top_count = len(scan.get("top_picks") or [])
        n_buy = sum(
            1 for c in scan["ranked_candidates"]
            if c.get("recommended_action") in (
                "open_new_research", "add_to_existing",
            )
        )
        stages["ranking"].update({
            "state": "done",
            "meta": (
                f"{len(scan['ranked_candidates'])} ranked · "
                f"{top_count} top picks"
                + (f" · {n_buy} BUY research" if n_buy else "")
            ),
        })

    # Briefs (every top pick should have a research_brief)
    top_picks = scan.get("top_picks") or []
    if top_picks:
        n_with_briefs = sum(1 for p in top_picks if p.get("research_brief"))
        stages["briefs"].update({
            "state": "done" if n_with_briefs == len(top_picks) else "running",
            "meta": f"{n_with_briefs}/{len(top_picks)} briefs",
        })
    elif scan:
        stages["briefs"].update({
            "state": "skipped",
            "meta": "no top picks (selectivity)",
        })

    # Downstream runs
    if downstream_ids:
        stages["downstream"].update({
            "state": "done",
            "meta": f"{len(downstream_ids)} runs spawned",
            "tooltip": ", ".join(downstream_ids),
        })
    elif scan and not top_picks:
        stages["downstream"].update({
            "state": "skipped",
            "meta": "nothing to route",
        })
    elif state.get("trigger_meta", {}).get("spawn_downstream") is False:
        stages["downstream"].update({
            "state": "skipped",
            "meta": "spawn disabled",
        })
    elif (run_status == "running"
          and state.get("trigger_meta", {}).get("spawn_downstream", True)
          and scan):
        stages["downstream"].update({
            "state": "running",
            "meta": "spawning Fundamental → Plan runs",
        })

    if run_status == "error":
        for k in ("downstream", "briefs", "ranking", "screening",
                   "api_discovery", "scan_init"):
            if stages[k]["state"] == "running":
                stages[k]["state"] = "error"

    tools_by_agent = traces.tools_by_agent(run_id) if run_id else {}
    _attach_scan_tools(stages, tools_by_agent)

    active_key: Optional[str] = None
    if scan_incomplete and run_id:
        tool_stage = _infer_scan_stage_from_tools(tools_by_agent)
        if tool_stage and stages.get(tool_stage, {}).get("state") != "done":
            stages[tool_stage].update({
                "state": "running",
                "meta": stages[tool_stage].get("meta") or "in progress",
            })
            active_key = tool_stage
        active_key = _mark_running_from_journal(
            stages, _SCAN_STAGES, run_id, run_status,
            agent_to_stage=_SCAN_AGENT_STAGE,
        ) or active_key

    stage_list = [stages[k] for k, _ in _SCAN_STAGES]
    done = sum(1 for s in stage_list if s["state"] == "done")
    total_relevant = sum(1 for s in stage_list if s["state"] != "skipped")
    all_tools: list[str] = []
    for tl in tools_by_agent.values():
        for t in tl:
            if t not in all_tools:
                all_tools.append(t)
    summary = f"{done} of {total_relevant} stages complete"
    if active_key and run_status == "running":
        active = stages[active_key]
        summary = f"In progress: {active['label']} — {active.get('meta', '')}"
    return {
        "title": "Idea scan pipeline",
        "stages": stage_list,
        "summary": summary,
        "active_stage_key": active_key,
        "tools_used": all_tools,
    }


# ---------- plan lifecycle ----------

def build_plan_pipeline(plan: dict) -> dict:
    """Pipeline for a single plan's lifecycle.

    A plan moves: draft → risk_review → hitl → active → closed (or rejected).
    The lifecycle is read from plan.status + plan.history.
    """
    status = plan.get("status") or "draft"
    history = plan.get("history") or []
    rejection = plan.get("rejection_reason") or ""

    stages: dict[str, dict] = {k: {"key": k, "label": label,
                                      "state": "pending", "meta": "", "tooltip": ""}
                                  for k, label in _PLAN_STAGES}

    # Draft is always done if we have a plan
    stages["draft"].update({
        "state": "done",
        "meta": plan.get("created_at", "")[:16],
    })

    # Helper — did any history entry come from this agent?
    by_agent = {h.get("agent"): h for h in history}
    risk_entry = by_agent.get("risk_officer")
    operator_entry = next((h for h in history
                            if h.get("agent") in ("operator", "smoke_tester",
                                                    "smoke")
                            or h.get("action") in ("approved", "rejected")),
                            None)

    # Risk review
    if status == "rejected" and not operator_entry:
        # Rejected by Risk before HITL
        stages["risk_review"].update({
            "state": "error",
            "meta": "rejected by Risk",
            "tooltip": rejection[:160],
        })
        for k in ("hitl", "active", "closed"):
            stages[k]["state"] = "skipped"
    elif status in ("pending_hitl", "active") or risk_entry or operator_entry:
        stages["risk_review"].update({
            "state": "done",
            "meta": (risk_entry or {}).get("action", "passed"),
        })

    # HITL
    if status == "pending_hitl":
        stages["hitl"].update({
            "state": "running",
            "meta": "awaiting operator",
        })
        for k in ("active", "closed"):
            stages[k]["state"] = "pending"
    elif status == "rejected" and operator_entry and operator_entry.get(
            "action") == "rejected":
        stages["hitl"].update({
            "state": "error",
            "meta": "operator rejected",
            "tooltip": rejection[:160],
        })
        for k in ("active", "closed"):
            stages[k]["state"] = "skipped"
    elif status == "active" or operator_entry and operator_entry.get(
            "action") == "approved":
        stages["hitl"].update({
            "state": "done",
            "meta": (operator_entry or {}).get("agent", "approved"),
        })
    elif risk_entry and risk_entry.get("action") == "routed_to_hitl":
        stages["hitl"].update({
            "state": "running",
            "meta": "routed by Risk",
        })

    # Active
    if status == "active":
        stages["active"].update({
            "state": "running",
            "meta": "position open",
            "tooltip": plan.get("approved_at", "")[:16],
        })
    elif status == "exited":
        stages["active"].update({
            "state": "done",
            "meta": "was active",
        })

    # Closed
    if status == "exited":
        stages["closed"].update({
            "state": "done",
            "meta": "exited",
        })
    elif status == "rejected":
        # closed by rejection — already covered above
        pass

    stage_list = [stages[k] for k, _ in _PLAN_STAGES]
    done = sum(1 for s in stage_list if s["state"] == "done")
    total_relevant = sum(1 for s in stage_list if s["state"] != "skipped")
    return {
        "title": "Plan lifecycle",
        "stages": stage_list,
        "summary": f"{done} of {total_relevant} stages complete",
    }


# ---------- dispatcher ----------

_MONITOR_STAGES = [
    ("monitor_init", "Monitor cycle"),
    ("evaluate", "Evaluate holdings"),
    ("actions", "Apply actions"),
    ("downstream", "Downstream runs"),
]


def build_monitor_pipeline(state: dict, run_status: str,
                           run_id: Optional[str] = None) -> dict:
    stages: dict[str, dict] = {
        k: {"key": k, "label": label, "state": "pending", "meta": "", "tooltip": ""}
        for k, label in _MONITOR_STAGES
    }
    stages["monitor_init"].update({
        "state": "done",
        "meta": f"spawn={state.get('trigger_meta', {}).get('spawn_pipeline', True)}",
    })
    evals = state.get("plan_evaluations") or []
    n = state.get("plans_evaluated") or state.get("positions_evaluated") or len(evals)
    total = state.get("supervision_plan_total")
    if total is None and evals:
        total = n
    current = state.get("supervision_current") or {}
    if run_status != "running":
        stages["evaluate"].update({
            "state": "done",
            "meta": (
                f"{n} plans · "
                f"{state.get('plans_healthy', state.get('positions_healthy', 0))} ok · "
                f"{state.get('plans_need_action', state.get('positions_action_required', 0))} action"
            ),
        })
    elif n or current:
        meta = (
            f"{len(evals)}/{total} plans" if total is not None
            else f"{n} plan(s) so far"
        )
        if current.get("ticker"):
            meta += f" · now: {current['ticker']}"
            phase = current.get("phase") or ""
            if phase:
                meta += f" ({phase})"
        stages["evaluate"].update({"state": "running", "meta": meta})
    else:
        stages["evaluate"].update({"state": "running", "meta": "checking plans…"})

    reports = state.get("position_reports") or []
    n_actions = sum(len(r.get("recommended_actions") or []) for r in reports)
    if reports:
        stages["actions"].update({
            "state": "done",
            "meta": f"{n_actions} action(s) applied",
        })
    spawned = state.get("spawned_run_ids") or []
    if spawned:
        stages["downstream"].update({
            "state": "done",
            "meta": f"{len(spawned)} pipeline run(s)",
            "tooltip": ", ".join(spawned),
        })
    elif reports and not n_actions:
        stages["downstream"].update({"state": "skipped", "meta": "no breaches"})
    elif state.get("trigger_meta", {}).get("spawn_pipeline") is False:
        stages["downstream"].update({"state": "skipped", "meta": "spawn disabled"})

    if run_status == "error":
        for k in ("downstream", "actions", "evaluate"):
            if stages[k]["state"] == "running":
                stages[k]["state"] = "error"

    active_key = _mark_running_from_journal(
        stages, _MONITOR_STAGES, run_id, run_status,
        agent_to_stage=_MONITOR_AGENT_STAGE,
    )
    if not active_key and stages["evaluate"]["state"] == "running":
        active_key = "evaluate"

    stage_list = [stages[k] for k, _ in _MONITOR_STAGES]
    done = sum(1 for s in stage_list if s["state"] == "done")
    total = sum(1 for s in stage_list if s["state"] != "skipped")
    summary = f"{done} of {total} stages complete"
    if active_key and run_status == "running":
        active = stages[active_key]
        summary = f"In progress: {active['label']} — {active.get('meta', '')}"

    return {
        "title": "Plan supervision pipeline",
        "stages": stage_list,
        "summary": summary,
        "active_stage_key": active_key,
    }


_BALANCE_STAGES = [
    ("book", "Firm snapshot"),
    ("manager", "Portfolio Manager"),
    ("orchestration", "Route to agents"),
    ("supervision", "Plan supervision"),
]


def build_firm_balance_pipeline(state: dict, run_status: str,
                                run_id: Optional[str] = None) -> dict:
    """Portfolio Manager balance cycle — not the news trading pipeline."""
    stages: dict[str, dict] = {
        k: {"key": k, "label": label, "state": "pending", "meta": "", "tooltip": ""}
        for k, label in _BALANCE_STAGES
    }
    meta = state.get("trigger_meta") or {}
    if state.get("firm_state"):
        stages["book"].update({"state": "done", "meta": "live book loaded"})

    mgr = state.get("firm_manager")
    if mgr:
        n_tasks = len(mgr.get("tasks") or [])
        stages["manager"].update({
            "state": "done",
            "meta": f"{n_tasks} policy task(s)",
            "tooltip": (mgr.get("reasoning_narrative") or "")[:200],
        })
    elif run_status == "running" and state.get("firm_state"):
        stages["manager"].update({"state": "running", "meta": "reading book…"})

    orch = state.get("manager_orchestration") or state.get("orchestration")
    if orch:
        spawned = orch.get("spawned_run_ids") or state.get("spawned_run_ids") or []
        n_act = len(orch.get("actions") or [])
        stages["orchestration"].update({
            "state": "done",
            "meta": f"{len(spawned)} run(s) · {n_act} action(s)",
            "tooltip": ", ".join(spawned[:8]) if spawned else "",
        })
    elif mgr and run_status == "running":
        stages["orchestration"].update({
            "state": "running",
            "meta": "spawning scan / reviews…",
        })

    if not meta.get("trigger_supervision", True):
        stages["supervision"].update({"state": "skipped", "meta": "disabled"})
    elif state.get("supervision_run_id"):
        sid = state["supervision_run_id"]
        stages["supervision"].update({
            "state": "done",
            "meta": sid[:18] + "…",
            "tooltip": f"/run/{sid}",
        })
    elif orch and run_status == "running" and meta.get("trigger_supervision", True):
        stages["supervision"].update({
            "state": "running",
            "meta": "supervising plans…",
        })

    if run_status in ("completed", "error"):
        final = state.get("final_status", "")
        if run_status == "completed":
            for k in ("book", "manager", "orchestration"):
                if stages[k]["state"] == "pending":
                    stages[k]["state"] = "done"
            if stages["supervision"]["state"] == "running":
                stages["supervision"]["state"] = (
                    "done" if state.get("supervision_run_id") else "skipped"
                )
        if run_status == "error":
            for _k, s in stages.items():
                if s["state"] == "running":
                    s["state"] = "error"
                    s["meta"] = final or "error"

    active_key = _mark_running_from_journal(
        stages, _BALANCE_STAGES, run_id, run_status,
        agent_to_stage=_BALANCE_AGENT_STAGE,
    )
    stage_list = [stages[k] for k, _ in _BALANCE_STAGES]
    done = sum(1 for s in stage_list if s["state"] == "done")
    total = sum(1 for s in stage_list if s["state"] != "skipped")
    summary = f"{done} of {total} stages complete"
    if active_key and run_status == "running":
        active = stages[active_key]
        summary = f"In progress: {active['label']} — {active.get('meta', '')}"

    return {
        "title": "Portfolio Manager — policy & routing",
        "stages": stage_list,
        "summary": summary,
        "active_stage_key": active_key,
    }


def build_for_run(state: dict, run_status: str,
                    trigger_type: Optional[str] = None,
                    run_id: Optional[str] = None) -> dict:
    """Pick the right builder based on the run's trigger_type."""
    tt = trigger_type or state.get("trigger_type", "")
    if tt == "idea_scan":
        return build_scan_pipeline(state, run_status, run_id=run_id)
    if tt in ("position_monitor", "plan_supervision"):
        return build_monitor_pipeline(state, run_status, run_id=run_id)
    if tt == "firm_balance":
        return build_firm_balance_pipeline(state, run_status, run_id=run_id)
    return build_news_pipeline(state, run_status, run_id=run_id)
