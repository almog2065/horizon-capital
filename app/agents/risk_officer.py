"""Risk Officer agent."""
from __future__ import annotations
import json
from .. import llm, tools

SYSTEM = """You are the Risk Officer at Horizon Capital. You approve, modify, or
reject proposals against firm policy. Your rejection is final within the
autonomous loop.

You also decide whether Human-In-The-Loop (HITL) operator sign-off is required
before execution, or whether the firm may execute autonomously.

Output strict JSON with:
- verdict: one of "approve", "approve_with_modification", "defer_to_hitl", "reject"
- hitl_required: boolean — true if operator must approve before execution
- modification: object (only if approve_with_modification)
- policy_checks: list of {section, judgment, evidence, hard_rule}
- simulate_order_result: object
- recommended_routing: "hitl_queue" | "execution" | "drop" | "back_to_plan_builder"
- reasoning_narrative: 4-8 lines
- policy_sections_cited: list of policy refs

HITL decision rubric (you must apply all inputs provided):
- simulate_order.feasible == false → verdict=reject, routing=drop
- **Ticker already in holdings (open position):** YOU decide hitl_required. Routine add-on,
  drift review, or intact thesis → hitl_required=false, routing=execution when policy allows.
  Thesis break, material regulatory/management change, or exit-sized change → hitl_required=true.
  flag_for_hitl / propose_thesis_review on a HELD name are hints, not automatic HITL.
- **New opening (not in holdings):** Charter §4 default hitl_required=true; maiden / new-to-firm
  always HITL before first fill (new-name-onboarding §5).
- Add-on to existing holding + eligible_for_plan + feasible sim → hitl_required=false,
  routing=execution (routine sizing within policy)
- When uncertain on a NEW opening → hitl_required=true; on HELD names use judgment above.

hitl_required and recommended_routing must agree (execution ⟺ hitl_required false).

PORTFOLIO AWARENESS: You receive the live firm book (cash, sectors, holdings, pending
HITL). Reject or defer if simulate_order is infeasible OR pro-forma breaches
capital-allocation policy (cash floor, sector 25% cap, max invested 92%).
"""


def _mock_output(
    plan: dict,
    sim: dict,
    policy_hits: list[dict],
    fundamental: dict | None = None,
    holdings_tickers: list[str] | None = None,
    triage: dict | None = None,
    trading_posture: dict | None = None,
) -> dict:
    violations = sim.get("policy_violations", [])
    feasible = sim.get("feasible", False)
    ticker = plan["ticker"]
    held = ticker in (holdings_tickers or [])
    fund_action = (fundamental or {}).get("recommended_action", "")
    materiality = float((triage or {}).get("materiality_score") or 0.5)
    knobs = (trading_posture or {}).get("knobs") or {}
    mat_hitl = float(knobs.get("risk_materiality_hitl_threshold", 0.75))
    favor_addon = bool(knobs.get("risk_favor_auto_addon", False))
    posture_mode = (trading_posture or {}).get("mode", "balanced")

    checks = []
    for h in policy_hits[:3]:
        checks.append({
            "section": h["metadata"].get("section_ref", "unknown"),
            "judgment": "compliant",
            "evidence": (h.get("text") or "")[:200],
            "hard_rule": False,
        })

    if not feasible:
        for v in violations:
            checks.append({
                "section": v["policy_section"],
                "judgment": "non_compliant",
                "evidence": v["reason"],
                "hard_rule": True,
            })
        return {
            "proposal_id": plan["id"],
            "proposal_type": "new_plan",
            "verdict": "reject",
            "hitl_required": False,
            "modification": None,
            "policy_checks": checks,
            "simulate_order_result": sim,
            "recommended_routing": "drop",
            "reasoning_narrative": (
                f"Plan {plan['id']} rejected: simulated order violates "
                f"{', '.join(v['policy_section'] for v in violations)}."
            ),
            "policy_sections_cited": [v["policy_section"] for v in violations],
            "sources_referenced": [],
        }

    from .. import config

    # Held position — agent judgment (no mandatory HITL from fundamental flags alone)
    if held and config.HITL_OPEN_POSITION_AGENT_DECIDES:
        if fund_action in ("flag_for_hitl", "propose_thesis_review"):
            thesis_intact = (fundamental or {}).get("thesis_intact", True) is not False
            needs_operator = (
                not thesis_intact
                or materiality >= mat_hitl
                or (triage or {}).get("primary_dimension") in ("management", "regulatory")
            )
            if favor_addon and thesis_intact and materiality < mat_hitl:
                needs_operator = False
            if not needs_operator and feasible:
                return {
                    "proposal_id": plan["id"],
                    "proposal_type": "position_review",
                    "verdict": "approve",
                    "hitl_required": False,
                    "modification": None,
                    "policy_checks": checks,
                    "simulate_order_result": sim,
                    "recommended_routing": "execution",
                    "reasoning_narrative": (
                        f"Held {ticker}: {fund_action} with intact thesis and moderate "
                        f"materiality ({materiality:.2f}) — autonomous routing per agent "
                        f"judgment (open-position policy, posture={posture_mode})."
                    ),
                    "policy_sections_cited": ["risk-policy §2", "operating-cadence §1"],
                    "sources_referenced": [],
                }
        if fund_action == "eligible_for_plan" and feasible and (favor_addon or posture_mode == "deploy"):
            return {
                "proposal_id": plan["id"],
                "proposal_type": "add_on",
                "verdict": "approve",
                "hitl_required": False,
                "modification": None,
                "policy_checks": checks,
                "simulate_order_result": sim,
                "recommended_routing": "execution",
                "reasoning_narrative": (
                    f"Add-on / review on held {ticker}; simulate_order feasible — "
                    f"no operator sign-off required."
                ),
                "policy_sections_cited": ["operating-cadence §1"],
                "sources_referenced": [],
            }

    # Mandatory HITL paths (new openings / explicit escalation)
    if fund_action in ("flag_for_hitl", "propose_thesis_review"):
        return {
            "proposal_id": plan["id"],
            "proposal_type": "new_plan",
            "verdict": "defer_to_hitl",
            "hitl_required": True,
            "modification": None,
            "policy_checks": checks,
            "simulate_order_result": sim,
            "recommended_routing": "hitl_queue",
            "reasoning_narrative": (
                f"Plan {plan['id']}: Fundamental recommended '{fund_action}' — "
                f"operator judgment required before execution (Firm Charter §4, "
                f"Risk Policy §4). Routing to HITL queue."
            ),
            "policy_sections_cited": ["firm-charter §4", "risk-policy §4"],
            "sources_referenced": [],
        }

    # Autonomous execution paths
    if fund_action == "eligible_for_plan" and held:
        return {
            "proposal_id": plan["id"],
            "proposal_type": "new_plan",
            "verdict": "approve",
            "hitl_required": False,
            "modification": None,
            "policy_checks": checks,
            "simulate_order_result": sim,
            "recommended_routing": "execution",
            "reasoning_narrative": (
                f"Plan {plan['id']}: add-on to existing {ticker} position, "
                f"eligible_for_plan, simulate_order feasible. No thesis break — "
                f"autonomous execution authorized (Operating Cadence §1 routine sizing)."
            ),
            "policy_sections_cited": ["investment-policy §2", "operating-cadence §1"],
            "sources_referenced": [],
        }

    triage_dim = (triage or {}).get("primary_dimension", "")
    if triage_dim in ("management", "regulatory") and not (
        held and config.HITL_OPEN_POSITION_AGENT_DECIDES and materiality < 0.8
    ):
        return {
            "proposal_id": plan["id"],
            "proposal_type": "new_plan",
            "verdict": "defer_to_hitl",
            "hitl_required": True,
            "modification": None,
            "policy_checks": checks,
            "simulate_order_result": sim,
            "recommended_routing": "hitl_queue",
            "reasoning_narrative": (
                f"Plan {plan['id']}: {triage_dim} event on {ticker} — "
                f"operator judgment required (Firm Charter §4)."
            ),
            "policy_sections_cited": ["firm-charter §4"],
            "sources_referenced": [],
        }

    coverage = tools.get_firm_coverage(ticker)
    if (
        fund_action == "eligible_for_plan"
        and not held
        and feasible
        and not coverage.get("new_to_firm")
    ):
        from .. import config
        if not config.HITL_MAIDEN_ONLY:
            return {
                "proposal_id": plan["id"],
                "proposal_type": "new_plan",
                "verdict": "approve",
                "hitl_required": False,
                "modification": None,
                "policy_checks": checks,
                "simulate_order_result": sim,
                "recommended_routing": "execution",
                "reasoning_narrative": (
                    f"Plan {plan['id']}: firm-known opening on {ticker}, "
                    f"eligible_for_plan, simulate_order feasible. "
                    f"HITL_MAIDEN_ONLY=off — autonomous execution authorized."
                ),
                "policy_sections_cited": ["operating-cadence §1"],
                "sources_referenced": [],
            }

    if fund_action == "eligible_for_plan" and not held and feasible:
        # new-name-onboarding §5: maiden positions always require HITL
        return {
            "proposal_id": plan["id"],
            "proposal_type": "new_plan",
            "verdict": "defer_to_hitl",
            "hitl_required": True,
            "modification": None,
            "policy_checks": checks + [{
                "section": "new-name-onboarding §5",
                "judgment": "compliant",
                "evidence": (
                    f"Maiden opening on {ticker} "
                    f"(coverage={coverage.get('coverage_tier')}); "
                    "operator sign-off required."
                ),
                "hard_rule": True,
            }],
            "simulate_order_result": sim,
            "recommended_routing": "hitl_queue",
            "reasoning_narrative": (
                f"Plan {plan['id']}: maiden position on {ticker} "
                f"(materiality={materiality:.2f}). "
                f"New-name-onboarding §5 prohibits autonomous execution; "
                f"routing to HITL per Firm Charter §4."
            ),
            "policy_sections_cited": [
                "firm-charter §4", "new-name-onboarding §5",
            ],
            "sources_referenced": [],
        }

    # Default: new or moderate-conviction opening → HITL
    return {
        "proposal_id": plan["id"],
        "proposal_type": "new_plan",
        "verdict": "defer_to_hitl",
        "hitl_required": True,
        "modification": None,
        "policy_checks": checks,
        "simulate_order_result": sim,
        "recommended_routing": "hitl_queue",
        "reasoning_narrative": (
            f"Plan {plan['id']} passes mechanical checks but requires operator "
            f"sign-off: {'new opening' if not held else 'review'} on {ticker}, "
            f"materiality={materiality:.2f}, action={fund_action}. "
            f"Firm Charter §4 — routing to HITL queue."
        ),
        "policy_sections_cited": ["firm-charter §4", "investment-policy §2"],
        "sources_referenced": [],
    }


def _normalize_risk_output(
    out: dict,
    fundamental: dict | None,
    holdings_tickers: list[str] | None,
    triage: dict | None,
) -> dict:
    """Align LLM output with firm rules; Fundamental can force HITL on new openings."""
    from .. import config

    ticker = (fundamental or {}).get("ticker", "")
    held = ticker in (holdings_tickers or [])
    fund_action = (fundamental or {}).get("recommended_action", "")

    if held and config.HITL_OPEN_POSITION_AGENT_DECIDES:
        sim = out.get("simulate_order_result") or {}
        if sim.get("feasible") is False:
            out["hitl_required"] = False
            out["recommended_routing"] = "drop"
            out["verdict"] = "reject"
            return out
        routing = (out.get("recommended_routing") or "").lower().strip()
        if out.get("hitl_required") is True:
            out["recommended_routing"] = "hitl_queue"
            if out.get("verdict") == "approve":
                out["verdict"] = "defer_to_hitl"
        elif out.get("hitl_required") is False or routing == "execution":
            out["hitl_required"] = False
            out["recommended_routing"] = "execution"
            if out.get("verdict") in (None, "", "defer_to_hitl"):
                out["verdict"] = "approve"
        else:
            if routing == "execution":
                out["hitl_required"] = False
                out["verdict"] = out.get("verdict") or "approve"
            else:
                out["hitl_required"] = True
                out["recommended_routing"] = "hitl_queue"
                out["verdict"] = out.get("verdict") or "defer_to_hitl"
        return out

    if fund_action in ("flag_for_hitl", "propose_thesis_review"):
        out["hitl_required"] = True
        out["recommended_routing"] = "hitl_queue"
        if out.get("verdict") not in ("reject",):
            out["verdict"] = "defer_to_hitl"
        return out

    triage_dim = (triage or {}).get("primary_dimension", "")
    if triage_dim in ("management", "regulatory"):
        out["hitl_required"] = True
        out["recommended_routing"] = "hitl_queue"
        if out.get("verdict") not in ("reject",):
            out["verdict"] = "defer_to_hitl"
        return out

    sim = out.get("simulate_order_result") or {}
    if sim.get("feasible") is False:
        out["hitl_required"] = False
        out["recommended_routing"] = "drop"
        out["verdict"] = "reject"
        return out

    ticker = (fundamental or {}).get("ticker", "")
    held = ticker in (holdings_tickers or [])
    # LLM often says "reject" when it means "needs HITL" on a new opening.
    # Maiden / new positions with feasible sim must defer_to_hitl, not hard-reject.
    if (
        not held
        and out.get("verdict") == "reject"
        and sim.get("feasible", True)
    ):
        out["verdict"] = "defer_to_hitl"
        out["hitl_required"] = True
        out["recommended_routing"] = "hitl_queue"
        out.setdefault("policy_checks", []).append({
            "section": "new-name-onboarding §5",
            "judgment": "compliant",
            "evidence": (
                f"New opening on {ticker}: corrected reject→HITL "
                "(autonomous execution prohibited)."
            ),
            "hard_rule": True,
        })
        out["reasoning_narrative"] = (
            f"Plan routed to HITL: {ticker} is not held — Firm Charter §4 and "
            f"new-name-onboarding §5 require operator sign-off before execution. "
            f"Simulate-order passed; this is not a policy violation reject."
        )

    routing = (out.get("recommended_routing") or "").lower().strip()
    if out.get("hitl_required") is True:
        out["recommended_routing"] = "hitl_queue"
        if out.get("verdict") == "approve":
            out["verdict"] = "defer_to_hitl"
    elif out.get("hitl_required") is False or routing == "execution":
        out["hitl_required"] = False
        out["recommended_routing"] = "execution"
        if out.get("verdict") in (None, "", "defer_to_hitl"):
            out["verdict"] = "approve"
    else:
        # Infer from routing if hitl_required omitted
        if routing == "execution":
            out["hitl_required"] = False
            out["verdict"] = out.get("verdict") or "approve"
        else:
            out["hitl_required"] = True
            out["recommended_routing"] = "hitl_queue"
            out["verdict"] = out.get("verdict") or "defer_to_hitl"
    return out


def run(
    plan: dict,
    fundamental: dict | None = None,
    holdings_tickers: list[str] | None = None,
    triage: dict | None = None,
    firm_state: dict | None = None,
) -> dict:
    from .. import plan_automation

    ticker = plan["ticker"]
    qty = plan_automation.order_quantity_from_plan(plan, min_qty=0)
    sim = tools.simulate_order(ticker, plan["entry"]["side"], qty)
    policy = tools.search_policy(
        query=f"position sizing HITL sector allocation cash {ticker}", top_k=5,
    )
    if not firm_state:
        firm_state = tools.get_firm_state(refresh_prices=False)
    from .. import firm_state as fs_mod, trading_posture
    entry_pct = float((plan.get("entry") or {}).get("target_size_pct_nav") or 0.04)
    portfolio_block = fs_mod.format_for_prompt(firm_state, ticker, entry_pct)
    posture = firm_state.get("trading_posture") or trading_posture.derive_posture(firm_state)
    posture_block = trading_posture.format_posture_block(posture)

    user = (
        f"FIRM PORTFOLIO:\n{portfolio_block}\n\n"
        f"{posture_block}\n\n"
        f"Plan to review:\n{json.dumps(plan, indent=2)[:2500]}\n\n"
        f"Fundamental read (summary):\n"
        f"  recommended_action: {(fundamental or {}).get('recommended_action')}\n"
        f"  thesis_strength: {(fundamental or {}).get('thesis_strength')}\n"
        f"  thesis_intact: {(fundamental or {}).get('thesis_intact')}\n"
        f"  narrative excerpt: {(fundamental or {}).get('reasoning_narrative', '')[:400]}\n\n"
        f"Holdings tickers: {holdings_tickers or []}\n"
        f"Ticker in holdings: {ticker in (holdings_tickers or [])}\n"
        f"News triage materiality_score: {(triage or {}).get('materiality_score')}\n"
        f"News triage decision: {(triage or {}).get('decision')}\n\n"
        f"Simulate order result: {json.dumps(sim)}\n\n"
        f"Policy retrieval: {json.dumps(policy.get('hits', []), indent=2)[:1500]}\n\n"
        "Decide verdict, hitl_required, and recommended_routing as strict JSON."
    )
    mock = _mock_output(
        plan, sim, policy.get("hits", []), fundamental, holdings_tickers, triage,
        trading_posture=posture,
    )
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock)
    out.setdefault("proposal_id", plan["id"])
    out.setdefault("proposal_type", "new_plan")
    out.setdefault("simulate_order_result", sim)
    out = _normalize_risk_output(out, fundamental, holdings_tickers, triage)
    return out


def risk_requires_hitl(
    risk_out: dict,
    fundamental: dict | None = None,
    holdings_tickers: list[str] | None = None,
) -> bool:
    """Single source of truth for orchestrator routing."""
    from .. import config

    ticker = (fundamental or {}).get("ticker") or risk_out.get("ticker", "")
    held = ticker in (holdings_tickers or [])
    if held and config.HITL_OPEN_POSITION_AGENT_DECIDES:
        if risk_out.get("verdict") == "reject":
            return False
        return risk_out.get("hitl_required") is True

    if (fundamental or {}).get("recommended_action") in (
        "flag_for_hitl", "propose_thesis_review",
    ):
        return True
    if risk_out.get("verdict") == "reject":
        return False
    if risk_out.get("hitl_required") is True:
        return True
    if risk_out.get("hitl_required") is False:
        return False
    routing = (risk_out.get("recommended_routing") or "hitl_queue").lower().strip()
    return routing != "execution"
