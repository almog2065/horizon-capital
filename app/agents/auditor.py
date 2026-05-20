"""Auditor agent. Reviews each agent's output after-the-fact."""
from __future__ import annotations

import json

from .. import llm

SYSTEM = """You are the Auditor at Horizon Capital. You evaluate completed agent
actions against firm policy. You audit the PROCESS, not the result.

Output strict JSON:
- compliant: bool
- findings: list of {finding_type, policy_section, deviation, evidence_ref, severity}
- overall_severity: "info" | "low" | "medium" | "high"
- recommended_action: "log_only" | "flag" | "freeze_plan" | "escalate_hitl"
- reasoning_narrative: 4-8 lines
- policy_sections_cited: list
"""


def _mock_output(agent_name: str, agent_output: dict) -> dict:
    findings = []

    # Procedural checks
    if not agent_output.get("policy_sections_cited"):
        findings.append({
            "finding_type": "policy_citation_missing",
            "policy_section": "risk-policy §5",
            "deviation": "Agent output missing policy_sections_cited.",
            "evidence_ref": agent_name,
            "severity": "medium",
        })

    srcs = agent_output.get("sources_referenced", [])
    if agent_name == "fundamental_analyst" and len(srcs) < 3:
        findings.append({
            "finding_type": "source_grounding_weak",
            "policy_section": "investment-policy §4",
            "deviation": f"Only {len(srcs)} sources cited; minimum is 3 for plan eligibility.",
            "evidence_ref": agent_name,
            "severity": "medium" if agent_output.get("recommended_action")
                        == "eligible_for_plan" else "low",
        })

    if agent_name == "plan_builder" and agent_output.get("status") == "drafted":
        plan = agent_output.get("plan", {})
        if not plan.get("monitoring", {}).get("checks"):
            findings.append({
                "finding_type": "step_skipped",
                "policy_section": "trading-plan schema",
                "deviation": "Plan drafted without monitoring.checks.",
                "evidence_ref": agent_name,
                "severity": "high",
            })

    if agent_name == "risk_officer":
        sim = agent_output.get("simulate_order_result", {})
        if (agent_output.get("verdict") == "approve"
                and not sim.get("feasible", False)):
            findings.append({
                "finding_type": "disproportionate_action",
                "policy_section": "risk-policy §6",
                "deviation": "Risk approved despite simulate_order infeasible.",
                "evidence_ref": agent_name,
                "severity": "high",
            })

    # Severity aggregation
    sev_order = {"info": 0, "low": 1, "medium": 2, "high": 3}
    if findings:
        overall = max(findings, key=lambda f: sev_order[f["severity"]])["severity"]
    else:
        overall = "info"

    action_map = {
        "info": "log_only",
        "low": "log_only",
        "medium": "flag",
        "high": "freeze_plan",
    }

    return {
        "compliant": len(findings) == 0,
        "findings": findings,
        "overall_severity": overall,
        "recommended_action": action_map[overall],
        "reasoning_narrative": (
            f"Audit of {agent_name}: {len(findings)} finding(s). "
            f"Highest severity: {overall}. "
            f"Recommended: {action_map[overall]}."
        ),
        "policy_sections_cited": ["risk-policy §5"],
    }


def run(agent_name: str, agent_output: dict, journal_id: int) -> dict:
    user = (
        f"Audited agent: {agent_name}\n"
        f"Agent output:\n{json.dumps(agent_output, indent=2)[:3000]}\n\n"
        "Audit as strict JSON."
    )
    mock = _mock_output(agent_name, agent_output)
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock)
    out.setdefault("about_journal_id", journal_id)
    out.setdefault("audited_agent", agent_name)
    out.setdefault("compliant", True)
    out.setdefault("overall_severity", "info")
    out.setdefault("recommended_action", "log_only")
    return out
