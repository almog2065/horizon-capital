"""Plan Supervisor — smart gate before execution and ongoing plan stewardship.

Reviews every monitored plan (with or without an open position). Decides
whether to monitor only, route to HITL, authorize execution, or trigger
the agent pipeline for re-evaluation. Never bypasses Firm Charter §4 or
new-name-onboarding §5.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from .. import config, llm, tools

SYSTEM = """You are the Plan Supervisor at Horizon Capital. You oversee trading
plans in all lifecycle phases: draft (not filled), pending_hitl, active
(filled or approved).

You receive mechanical pre-checks and (if applicable) position monitor output.
Your verdict controls whether the firm may EXECUTE, must wait for HITL, or
should re-open the analyst pipeline.

RULES (hard):
- pending_hitl → verdict=awaiting_operator, execution_authorized=false, hitl_required=true

OPEN POSITION (phase=in_position, quantity>0):
- YOU decide hitl_required. Default: monitor_only, hitl_required=false, pipeline_spawn only
  when monitor shows action_required.
- Route to HITL only when thesis break, exit/trim material to NAV, or monitor explicitly sets
  hitl_required=true. Routine checks (log, soft drift) do not require operator queue.
- Respect position_monitor.hitl_required when present.

PORTFOLIO AWARENESS: Use firm_state (cash, sector weights, position count) when
authorizing execution or routing to HITL. Do not authorize entries that breach
capital-allocation §1–§4 in pro-forma.
- Maiden / new-to-firm opening → execution_authorized=false unless operator_approved in history
- Firm Charter §4: new openings need HITL unless add-on to existing held position with risk approve
- Full exit / thesis-break sell → prefer hitl_required=true unless monitor+risk clear autonomous
- sim_order infeasible or stale data → execution_authorized=false
- If monitor reports action_required → prefer trigger_pipeline over execution
- draft without risk clearance in context → do not authorize execution; use awaiting_operator or monitor_only

Output strict JSON:
- plan_id, ticker, phase: pre_position | in_position
- plan_status: draft | pending_hitl | active
- verdict: monitor_only | awaiting_operator | authorize_execution | trigger_pipeline | freeze_plan
- execution_authorized: bool
- hitl_required: bool
- pipeline_spawn: bool
- recommended_actions: list of {action, rationale}
- reasoning_narrative: 4-8 lines
- policy_sections_cited: list
"""

MONITORED_STATUSES = frozenset({"draft", "pending_hitl", "active"})


def _operator_approved(plan: dict) -> bool:
    for h in reversed(plan.get("history") or []):
        if h.get("action") in ("approved", "operator_hitl_approved"):
            return True
        if h.get("agent") in ("operator", "operator_hitl"):
            if h.get("action") == "approved":
                return True
    return False


def _pre_position_checks(plan: dict) -> dict:
    ticker = plan["ticker"]
    quote = tools.fetch_quote(ticker)
    entry = float(
        (plan.get("entry") or {}).get("entry_price_or_trigger", {}).get("value") or 0
    )
    pct = float((plan.get("entry") or {}).get("target_size_pct_nav") or 0.04)
    nav = config.STARTING_NAV
    price = float(quote.get("price") or entry or 1)
    qty = int((pct * nav) / price) if price else 0
    sim = tools.simulate_order(ticker, "long", qty, price)
    coverage = tools.get_firm_coverage(ticker)
    mcap = float(tools.fetch_fundamentals(ticker).get("market_cap_usd") or 0)

    window = int((plan.get("entry") or {}).get("execution_window_days") or 5)
    created = plan.get("created_at", "")
    try:
        age_days = (
            time.time() - time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
        ) / 86400
    except Exception:
        age_days = 0
    window_ok = age_days <= window

    checks = {
        "quote_ok": not quote.get("_data_unavailable"),
        "sim_feasible": bool(sim.get("feasible")),
        "entry_window_ok": window_ok,
        "market_cap_ok": mcap >= 5e9,
        "data_available": not quote.get("_data_unavailable"),
    }
    issues = []
    if not checks["quote_ok"]:
        issues.append("quote_unavailable")
    if not checks["sim_feasible"]:
        issues.append("sim_infeasible")
    if not checks["entry_window_ok"]:
        issues.append("entry_window_expired")
    if not checks["market_cap_ok"]:
        issues.append("market_cap_below_floor")

    return {
        "checks": checks,
        "issues": issues,
        "simulate_order": sim,
        "quote": {"price": price, "source": quote.get("_source")},
        "coverage": coverage,
        "execution_ready": all(checks.values()) and not issues,
    }


def _mock_output(
    plan: dict,
    plan_status: str,
    phase: str,
    holding: Optional[dict],
    pre_check: dict,
    monitor_report: Optional[dict],
    risk_context: Optional[dict],
    trading_posture: Optional[dict] = None,
) -> dict:
    ticker = plan["ticker"]
    plan_id = plan["id"]
    coverage = pre_check.get("coverage") or tools.get_firm_coverage(ticker)
    held = bool(holding and int(holding.get("quantity") or 0) > 0)
    maiden = not held and coverage.get("new_to_firm", True)
    exec_ready = pre_check.get("execution_ready", False)
    op_ok = _operator_approved(plan)

    risk_verdict = (risk_context or {}).get("risk_verdict")
    risk_hitl = (risk_context or {}).get("hitl_required")
    operator_approved = bool((risk_context or {}).get("operator_approved"))
    favor_auto = bool((trading_posture or {}).get("knobs", {}).get("supervisor_favor_auto_execute"))
    posture_mode = (trading_posture or {}).get("mode", "balanced")

    actions: list[dict] = []
    pipeline_spawn = False
    hitl_required = False
    execution_authorized = False
    verdict = "monitor_only"

    if operator_approved and exec_ready:
        verdict = "authorize_execution"
        execution_authorized = True
        hitl_required = False
        actions.append({
            "action": "execute_entry",
            "rationale": "Operator approved at HITL; pre-checks pass.",
        })

    elif plan_status == "pending_hitl":
        verdict = "awaiting_operator"
        hitl_required = True
        actions.append({
            "action": "await_hitl",
            "rationale": "Plan awaits operator sign-off per Firm Charter §4.",
        })

    elif plan_status == "draft":
        if not exec_ready:
            verdict = "freeze_plan"
            actions.append({
                "action": "freeze",
                "rationale": f"Pre-check failed: {pre_check.get('issues')}.",
            })
        elif maiden:
            verdict = "awaiting_operator"
            hitl_required = True
            actions.append({
                "action": "route_hitl",
                "rationale": "Maiden/new-to-firm — HITL required before first fill.",
            })
        else:
            verdict = "awaiting_operator"
            hitl_required = True
            actions.append({
                "action": "route_hitl",
                "rationale": "Draft plan needs risk + operator path before execution.",
            })

    elif plan_status == "active":
        if held:
            mon = monitor_report or {}
            mon_status = mon.get("overall_status", "healthy")
            mon_hitl = mon.get("hitl_required")
            if mon_status == "action_required":
                verdict = "trigger_pipeline"
                pipeline_spawn = True
                hitl_required = bool(mon_hitl)
                for a in mon.get("recommended_actions") or []:
                    if a.get("action") in ("trigger_re_eval", "review"):
                        actions.append(a)
                    if a.get("hitl_required") is True:
                        hitl_required = True
            else:
                verdict = "monitor_only"
                hitl_required = bool(mon_hitl) if mon_hitl is not None else False
        else:
            # Approved plan, no fill yet
            if exec_ready and op_ok and not maiden:
                verdict = "authorize_execution"
                execution_authorized = True
                actions.append({
                    "action": "execute_entry",
                    "rationale": "Active plan, no position, pre-checks pass, operator approved.",
                })
            elif exec_ready and maiden:
                verdict = "awaiting_operator"
                hitl_required = True
                actions.append({
                    "action": "route_hitl",
                    "rationale": "Maiden entry requires HITL before fill.",
                })
            elif exec_ready and risk_verdict == "approve" and not risk_hitl and not maiden:
                verdict = "authorize_execution"
                execution_authorized = True
            elif (
                favor_auto
                and exec_ready
                and risk_verdict in ("approve", "approve_with_modification")
                and not risk_hitl
                and not maiden
                and posture_mode in ("deploy", "diversify")
            ):
                verdict = "authorize_execution"
                execution_authorized = True
                actions.append({
                    "action": "execute_entry",
                    "rationale": (
                        f"Deploy posture ({posture_mode}): risk cleared, non-maiden — "
                        "authorize autonomous fill."
                    ),
                })
            else:
                verdict = "awaiting_operator"
                hitl_required = not op_ok

    # Risk context from live news run
    if risk_context and risk_verdict == "reject":
        verdict = "freeze_plan"
        execution_authorized = False
        hitl_required = False

    from .. import config
    if (
        risk_context and risk_hitl and not op_ok
        and not (held and config.HITL_OPEN_POSITION_AGENT_DECIDES)
    ):
        hitl_required = True
        if execution_authorized:
            verdict = "awaiting_operator"
            execution_authorized = False

    return {
        "plan_id": plan_id,
        "ticker": ticker,
        "phase": phase,
        "plan_status": plan_status,
        "verdict": verdict,
        "execution_authorized": execution_authorized,
        "hitl_required": hitl_required,
        "pipeline_spawn": pipeline_spawn,
        "recommended_actions": actions,
        "reasoning_narrative": (
            f"Supervisor: {ticker} plan {plan_id} status={plan_status} phase={phase}. "
            f"Held={held}, maiden={maiden}, exec_ready={exec_ready}. "
            f"Verdict={verdict}, execute={execution_authorized}, hitl={hitl_required}."
        ),
        "policy_sections_cited": [
            "firm-charter §4", "new-name-onboarding §5",
            "investment-policy §5", "operating-cadence §1",
        ],
        "pre_check": pre_check,
    }


def run(
    plan: dict,
    plan_status: str,
    phase: str,
    holding: Optional[dict] = None,
    monitor_report: Optional[dict] = None,
    risk_context: Optional[dict] = None,
    as_of: str = "",
    firm_state: Optional[dict] = None,
    manager_out: Optional[dict] = None,
) -> dict:
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    pre_check = _pre_position_checks(plan)
    if not firm_state:
        firm_state = tools.get_firm_state(refresh_prices=False)
    from .. import trading_posture
    posture = firm_state.get("trading_posture") or trading_posture.derive_posture(firm_state)
    mock = _mock_output(
        plan, plan_status, phase, holding, pre_check, monitor_report, risk_context,
        trading_posture=posture,
    )
    mock["as_of"] = as_of
    from .. import firm_state as fs_mod
    entry_pct = float((plan.get("entry") or {}).get("target_size_pct_nav") or 0.04)
    portfolio_block = fs_mod.format_for_prompt(
        firm_state, plan.get("ticker"), entry_pct,
    )

    from . import firm_manager
    manager_block = firm_manager.format_directives_block(manager_out)
    posture_block = trading_posture.format_posture_block(
        (manager_out or {}).get("trading_posture") or posture,
    )

    user = (
        f"Plan status: {plan_status}\nPhase: {phase}\nAs of: {as_of}\n\n"
        f"FIRM PORTFOLIO:\n{portfolio_block}\n\n"
        f"{posture_block}\n\n"
        f"{manager_block}\n\n"
        f"Plan excerpt:\n{json.dumps(plan, indent=2)[:2000]}\n\n"
        f"Holding: {json.dumps(holding) if holding else 'none'}\n\n"
        f"Pre-checks:\n{json.dumps(pre_check, indent=2)}\n\n"
        f"Monitor report:\n{json.dumps(monitor_report or {}, indent=2)[:2500]}\n\n"
        f"Risk context:\n{json.dumps(risk_context or {})}\n\n"
        "Return supervisor verdict as strict JSON."
    )
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock, purpose="plan_supervisor")
    out.setdefault("plan_id", plan["id"])
    out.setdefault("ticker", plan["ticker"])
    out.setdefault("phase", phase)
    out.setdefault("plan_status", plan_status)

    # Hard overrides
    if (risk_context or {}).get("operator_approved") and pre_check.get("execution_ready"):
        out["verdict"] = "authorize_execution"
        out["execution_authorized"] = True
        out["hitl_required"] = False
    elif plan_status == "pending_hitl":
        out["verdict"] = "awaiting_operator"
        out["execution_authorized"] = False
        out["hitl_required"] = True
    if not pre_check.get("execution_ready") and plan_status in ("draft", "active"):
        if out.get("execution_authorized"):
            out["execution_authorized"] = False
            out["verdict"] = "freeze_plan"
    cov = pre_check.get("coverage") or {}
    held = bool(holding and int(holding.get("quantity") or 0) > 0)
    if cov.get("new_to_firm") and not held and not _operator_approved(plan):
        out["execution_authorized"] = False
        out["hitl_required"] = True
        if out.get("verdict") == "authorize_execution":
            out["verdict"] = "awaiting_operator"

    from .. import config
    if (monitor_report or {}).get("overall_status") == "action_required":
        out["pipeline_spawn"] = True
        if out.get("verdict") == "authorize_execution":
            out["verdict"] = "trigger_pipeline"
            out["execution_authorized"] = False
        if held and config.HITL_OPEN_POSITION_AGENT_DECIDES:
            mon_hitl = (monitor_report or {}).get("hitl_required")
            if mon_hitl is False:
                out["hitl_required"] = False
            elif mon_hitl is True:
                out["hitl_required"] = True

    return out
