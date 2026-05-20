"""Firm-wide trading posture — manager-derived guidance per agent.

When the book is under-invested / under-diversified vs policy, agents bias toward
deployment (more Risk auto-approvals on routine add-ons, more scans). When
over-invested or concentrated, bias toward review and HITL.
"""
from __future__ import annotations

from typing import Any, Literal

from . import allocation

PostureMode = Literal["deploy", "diversify", "balanced", "constrained", "defensive"]


def derive_posture(firm_state: dict) -> dict[str, Any]:
    """Compute posture + per-agent guidance from live book vs capital-allocation policy."""
    policy = firm_state.get("policy") or {}
    deploy = firm_state.get("deployment_needs") or {}
    invested = float(firm_state.get("invested_pct", 0))
    cash = float(firm_state.get("cash_pct", 0))
    pos_n = int(firm_state.get("positions_count", 0))
    min_inv = float(policy.get("min_invested_pct", allocation.MIN_INVESTED_PCT))
    max_inv = float(policy.get("max_invested_pct", allocation.MAX_INVESTED_PCT))
    cash_ceil = float(policy.get("cash_ceiling_pct", allocation.CASH_CEILING_PCT))
    cash_floor = float(policy.get("cash_floor_pct", allocation.CASH_FLOOR_PCT))
    min_names = int(policy.get("min_position_count", allocation.TARGET_POSITION_COUNT[0]))

    conc = firm_state.get("concentration") or []
    over_cap = any(r.get("status") == "over_cap" for r in conc)
    approaching = any(r.get("status") == "approaching_cap" for r in conc)

    underweight_sectors = [
        r for r in firm_state.get("sectors", [])
        if float(r.get("pct_nav", 0)) < float(r.get("band_low_pct", 0)) - 0.01
    ]
    overweight_sectors = [
        r for r in firm_state.get("sectors", [])
        if float(r.get("pct_nav", 0)) > float(r.get("band_high_pct", 1))
    ]

    gaps: list[str] = []
    if deploy.get("need_deploy"):
        gaps.append("under_invested")
    if deploy.get("need_diversify"):
        gaps.append("under_diversified")
    if cash > cash_ceil:
        gaps.append("excess_cash")
    if invested > max_inv:
        gaps.append("over_invested")
    if over_cap:
        gaps.append("single_name_over_cap")
    if cash < cash_floor + 0.01:
        gaps.append("cash_near_floor")

    if cash < cash_floor + 0.005 or over_cap:
        mode: PostureMode = "defensive"
    elif invested >= max_inv - 0.02 or (approaching and not deploy.get("active")):
        mode = "constrained"
    elif deploy.get("need_deploy") and deploy.get("need_diversify"):
        mode = "deploy"
    elif deploy.get("need_diversify"):
        mode = "diversify"
    elif deploy.get("need_deploy"):
        mode = "deploy"
    else:
        mode = "balanced"

    knobs = _knobs_for_mode(mode)
    agent_guidance = _agent_guidance(
        mode, gaps, invested, cash, pos_n, min_inv, min_names,
        len(underweight_sectors), len(overweight_sectors), knobs,
    )

    return {
        "mode": mode,
        "summary": _summary(mode, gaps, invested, cash, pos_n, min_names),
        "policy_gaps": gaps,
        "knobs": knobs,
        "agent_guidance": agent_guidance,
        "underweight_sector_count": len(underweight_sectors),
        "overweight_sector_count": len(overweight_sectors),
    }


def _knobs_for_mode(mode: PostureMode) -> dict[str, Any]:
    if mode == "deploy":
        return {
            "risk_favor_auto_addon": True,
            "risk_materiality_hitl_threshold": 0.88,
            "supervisor_favor_auto_execute": True,
            "idea_scan_relax_floors": True,
            "monitor_review_intensity": "high",
            "max_new_openings_boost": 3,
        }
    if mode == "diversify":
        return {
            "risk_favor_auto_addon": True,
            "risk_materiality_hitl_threshold": 0.80,
            "supervisor_favor_auto_execute": False,
            "idea_scan_relax_floors": True,
            "monitor_review_intensity": "medium",
            "max_new_openings_boost": 2,
        }
    if mode == "constrained":
        return {
            "risk_favor_auto_addon": False,
            "risk_materiality_hitl_threshold": 0.65,
            "supervisor_favor_auto_execute": False,
            "idea_scan_relax_floors": False,
            "monitor_review_intensity": "high",
            "max_new_openings_boost": -2,
        }
    if mode == "defensive":
        return {
            "risk_favor_auto_addon": False,
            "risk_materiality_hitl_threshold": 0.55,
            "supervisor_favor_auto_execute": False,
            "idea_scan_relax_floors": False,
            "monitor_review_intensity": "high",
            "max_new_openings_boost": -3,
        }
    return {
        "risk_favor_auto_addon": True,
        "risk_materiality_hitl_threshold": 0.75,
        "supervisor_favor_auto_execute": False,
        "idea_scan_relax_floors": False,
        "monitor_review_intensity": "normal",
        "max_new_openings_boost": 0,
    }


def _summary(
    mode: PostureMode,
    gaps: list[str],
    invested: float,
    cash: float,
    pos_n: int,
    min_names: int,
) -> str:
    gap_txt = ", ".join(gaps) if gaps else "none"
    return (
        f"Trading posture={mode}: invested {invested:.1%}, cash {cash:.1%}, "
        f"{pos_n} names (min {min_names}). Policy gaps: {gap_txt}."
    )


def _agent_guidance(
    mode: PostureMode,
    gaps: list[str],
    invested: float,
    cash: float,
    pos_n: int,
    min_inv: float,
    min_names: int,
    n_under: int,
    n_over: int,
    knobs: dict,
) -> dict[str, str]:
    base = {
        "firm_manager": (
            f"Posture {mode}: route Idea Scan and supervision to close policy gaps "
            f"({', '.join(gaps) or 'balanced book'}). Set scan_directives accordingly."
        ),
        "idea_generator": (
            "Bias ranking toward underweight sectors and new names when posture is "
            "deploy/diversify; respect freeze_new_entries tasks."
        ),
        "fundamental": (
            "Cite sector headroom and portfolio fit. Under deploy posture, favor "
            "eligible_for_plan on names with dossier coverage in underweight sleeves."
        ),
        "plan_builder": (
            "Size to min(requested, sector headroom, policy cap). Deploy posture: "
            "prefer full policy-sized entries in underweight sectors."
        ),
        "risk_officer": (
            "Apply HITL rubric with current posture knobs. Held add-ons: "
            + (
                "favor autonomous approve when sim feasible and thesis intact."
                if knobs.get("risk_favor_auto_addon")
                else "require HITL on material changes; routine add-ons may auto-execute."
            )
        ),
        "plan_supervisor": (
            "Routine in-position monitoring → monitor_only without HITL unless "
            "thesis break. "
            + (
                "Deploy posture: authorize execution on approved non-maiden drafts when risk cleared."
                if knobs.get("supervisor_favor_auto_execute")
                else "Default HITL on maiden and material exits."
            )
        ),
        "position_monitor": (
            f"Review intensity={knobs.get('monitor_review_intensity')}. "
            "Flag concentration >7% NAV; route trim paths when over 8%."
        ),
        "operator": (
            "Prioritize HITL queue items in underweight sectors when posture is deploy; "
            "defer new maiden entries when constrained/defensive."
        ),
    }
    if mode == "deploy":
        base["idea_generator"] += (
            f" Book under-invested ({invested:.1%} < {min_inv:.0%}) — "
            f"raise eligible_for_plan rate for quality names in {n_under} underweight sectors."
        )
        base["risk_officer"] += (
            f" Materiality below {knobs['risk_materiality_hitl_threshold']:.0%} on held names "
            "→ prefer hitl_required=false."
        )
    elif mode in ("constrained", "defensive"):
        base["risk_officer"] += (
            " Elevated scrutiny: defer_to_hitl on new openings unless small add-on to held name."
        )
        base["idea_generator"] += " Deprioritize new names; prefer watch and trim reviews."
    elif mode == "diversify":
        base["idea_generator"] += (
            f" Only {pos_n} names (need ≥{min_names}) — prioritize open_new_research over add-ons."
        )
    return base


def format_posture_block(posture: dict | None) -> str:
    if not posture:
        return ""
    lines = [
        f"TRADING POSTURE ({posture.get('mode', 'balanced')}):",
        posture.get("summary", ""),
        "Per-agent guidance:",
    ]
    for agent, text in (posture.get("agent_guidance") or {}).items():
        lines.append(f"  • {agent}: {text}")
    knobs = posture.get("knobs") or {}
    if knobs:
        lines.append(
            "Mechanical knobs: "
            + ", ".join(f"{k}={v}" for k, v in knobs.items())
        )
    return "\n".join(lines)


def merge_scan_directives(scan_directives: dict, posture: dict | None) -> dict:
    """Boost manager scan caps when book needs deployment."""
    if not posture:
        return scan_directives
    sd = dict(scan_directives or {})
    boost = int((posture.get("knobs") or {}).get("max_new_openings_boost", 0))
    if boost and sd.get("max_new_openings") is not None:
        sd["max_new_openings"] = max(0, int(sd["max_new_openings"]) + boost)
    mode = posture.get("mode")
    if mode in ("deploy", "diversify") and not sd.get("deploy_urgency"):
        sd["deploy_urgency"] = "high" if mode == "deploy" else "medium"
    return sd
