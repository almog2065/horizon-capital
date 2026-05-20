"""Build operator-facing HITL briefs: what happened, approve headline, pros/cons."""
from __future__ import annotations

import json
from typing import Any, Optional

from . import config, db, pipeline_view, plan_automation, tools

# Pipeline order for HITL review (news → plan → risk)
_AGENT_PIPELINE: list[tuple[str, str, str, str]] = [
    ("news_triage", "triage", "News Triage", "⚡"),
    ("fundamental_analyst", "fundamental", "Fundamental Analyst", "📊"),
    ("plan_builder", "plan_draft", "Plan Builder", "📋"),
    ("risk_officer", "risk", "Risk Officer", "🛡"),
    ("plan_supervisor", "supervisor", "Plan Supervisor", "👁"),
    ("firm_manager", "firm_manager", "Portfolio Manager", "⚖"),
    ("execution", "fill", "Execution", "✅"),
]

_EXTRA_STATE_AGENTS: list[tuple[str, str, str, str]] = [
    ("idea_generator", "idea_scan", "Idea Generator", "💡"),
]


def _holding_for_ticker(ticker: str) -> Optional[dict]:
    for h in db.list_holdings():
        if h["ticker"] == ticker and int(h.get("quantity") or 0) > 0:
            return h
    return None


def _trigger_story(state: dict, run: Optional[dict]) -> list[dict]:
    """Timeline steps for 'what happened'."""
    steps: list[dict] = []
    tt = (run or {}).get("trigger_type", state.get("trigger_type", ""))
    news = state.get("news_item") or {}

    if tt == "news_event" and news:
        src = news.get("source", "news")
        steps.append({
            "icon": "📰",
            "title": "News / event",
            "detail": (news.get("headline") or "")[:160],
            "meta": src,
        })
    elif tt == "idea_scan":
        scan = state.get("idea_scan") or {}
        picks = scan.get("top_picks") or []
        score = picks[0].get("composite_score") if picks else None
        steps.append({
            "icon": "💡",
            "title": "Idea scan pick",
            "detail": (scan.get("reasoning_narrative") or "Selected from universe scan.")[:160],
            "meta": f"score {score:.2f}" if score is not None else "scan",
        })
    elif tt in ("plan_supervision", "position_monitor"):
        steps.append({
            "icon": "📡",
            "title": "Plan supervision",
            "detail": "Monitor flagged plan for operator review.",
            "meta": tt,
        })

    triage = state.get("triage") or {}
    if triage.get("decision"):
        steps.append({
            "icon": "⚡",
            "title": "News triage",
            "detail": f"Decision: {triage.get('decision')} · "
                      f"materiality {float(triage.get('materiality_score') or 0):.2f}",
            "meta": triage.get("primary_dimension", ""),
        })

    fund = state.get("fundamental") or {}
    if fund.get("recommended_action"):
        steps.append({
            "icon": "📊",
            "title": "Fundamental",
            "detail": (fund.get("reasoning_narrative") or "")[:140],
            "meta": fund.get("recommended_action", ""),
        })

    plan_draft = state.get("plan_draft") or {}
    if plan_draft.get("status") == "drafted" or plan_draft.get("plan"):
        steps.append({
            "icon": "📋",
            "title": "Plan drafted",
            "detail": (plan_draft.get("reasoning_narrative") or "Entry plan ready.")[:140],
            "meta": "drafted",
        })

    risk = state.get("risk") or {}
    if risk.get("verdict"):
        steps.append({
            "icon": "🛡",
            "title": "Risk Officer",
            "detail": (risk.get("reasoning_narrative") or "")[:140],
            "meta": f"{risk.get('verdict')} · HITL={risk.get('hitl_required')}",
        })

    sup = state.get("supervisor") or {}
    if sup.get("verdict"):
        steps.append({
            "icon": "👁",
            "title": "Plan Supervisor",
            "detail": (sup.get("reasoning_narrative") or "")[:140],
            "meta": sup.get("verdict", ""),
        })

    return steps


def _market_panel(ticker: str, plan: dict) -> dict:
    quote = tools.fetch_quote(ticker)
    price = float(quote.get("price") or 0)
    entry = float(
        (plan.get("entry") or {}).get("entry_price_or_trigger", {}).get("value") or 0
    )
    holding = _holding_for_ticker(ticker)
    h = holding or plan_automation.holding_for_plan(plan)
    qty = int((h or {}).get("quantity") or 0)
    cost = float((h or {}).get("cost_basis") or entry or price)
    ret = ((price - cost) / cost) if cost else 0.0
    nav = config.STARTING_NAV
    mv = qty * price if qty else nav * float(
        (plan.get("entry") or {}).get("target_size_pct_nav") or 0.04
    )
    pct_nav = mv / nav if nav else 0

    pe_ttm = None
    try:
        fund = tools.fetch_fundamentals(ticker)
        if fund.get("_source") != "error":
            pe_ttm = fund.get("pe_ttm")
    except Exception:
        pass
    return {
        "price": price,
        "entry_price": entry or cost,
        "return_pct": ret,
        "pct_nav": pct_nav,
        "quantity": qty,
        "has_position": qty > 0,
        "pe_ttm": pe_ttm,
        "quote_source": quote.get("_source", ""),
    }


def _pros_cons(
    plan: dict,
    state: dict,
    market: dict,
    position_type: str,
) -> tuple[list[str], list[str]]:
    plan["ticker"]
    entry_pct = float((plan.get("entry") or {}).get("target_size_pct_nav") or 0.04)
    fund = state.get("fundamental") or {}
    risk = state.get("risk") or {}

    pros: list[str] = []
    cons: list[str] = []

    if fund.get("recommended_action") in ("eligible_for_plan", "flag_for_hitl"):
        pros.append("Fundamental analyst supports opening or adding to the position.")
    if risk.get("verdict") in ("approve", "defer_to_hitl", "approve_with_modification"):
        pros.append("Risk Officer did not reject — policy checks passed at sim level.")
    if (risk.get("simulate_order_result") or {}).get("feasible"):
        pros.append("Simulated order is feasible (size, cash, sector caps).")
    if market.get("return_pct", 0) > 0 and market.get("has_position"):
        pros.append(f"Existing position is up {market['return_pct']:.1%} — momentum supportive.")
    elif not market.get("has_position"):
        pros.append(f"New entry sized at {entry_pct:.1%} NAV within policy band.")

    thesis = (plan.get("thesis") or {}).get("narrative", "")
    if thesis and len(thesis) > 40:
        pros.append("Documented thesis with supporting points in the plan.")

    if position_type == "maiden":
        cons.append("Maiden position — first time holding this name; no live track record.")
        cons.append("Firm Charter §4 requires your sign-off; no autonomous entry.")
    if position_type == "add_on":
        cons.append("Increases concentration — verify sector headroom after fill.")

    if market.get("return_pct", 0) < -0.10 and market.get("has_position"):
        cons.append(f"Position underwater {market['return_pct']:.1%} — timing risk on add.")
    if fund.get("recommended_action") == "flag_for_hitl":
        cons.append("Fundamental flagged uncertainty — not a clean green light.")
    if (risk.get("policy_checks") or []):
        hard = [c for c in risk["policy_checks"] if c.get("hard_rule")]
        if hard:
            cons.append(f"{len(hard)} hard policy check(s) noted — read Risk narrative.")

    if not pros:
        pros.append("Proceed only if you agree with thesis after reading full run journal.")
    if not cons:
        cons.append("Standard market and liquidity risk on any equity entry.")

    return pros[:5], cons[:5]


def _key_fields_for_agent(agent: str, output: dict) -> list[dict[str, str]]:
    """Structured highlights for the operator (not full JSON)."""
    fields: list[dict[str, str]] = []
    if not output:
        return fields

    if agent == "news_triage":
        fields.extend([
            {"label": "Decision", "value": str(output.get("decision", "—"))},
            {"label": "Materiality", "value": f"{float(output.get('materiality_score') or 0):.2f}"},
            {"label": "Dimension", "value": str(output.get("primary_dimension", "—"))},
        ])
        if output.get("impacted_tickers"):
            fields.append({
                "label": "Tickers",
                "value": ", ".join(output["impacted_tickers"][:6]),
            })
    elif agent == "fundamental_analyst":
        fields.extend([
            {"label": "Action", "value": str(output.get("recommended_action", "—"))},
            {"label": "Confidence", "value": str(output.get("confidence", "—"))},
        ])
        cites = output.get("policy_sections_cited") or []
        if cites:
            fields.append({"label": "Policy", "value": ", ".join(cites[:4])})
    elif agent == "plan_builder":
        status = output.get("status") or (output.get("plan") or {}).get("status")
        fields.append({"label": "Status", "value": str(status or "—")})
        plan = output.get("plan") or output
        entry = (plan.get("entry") or {}) if isinstance(plan, dict) else {}
        if entry.get("target_size_pct_nav") is not None:
            fields.append({
                "label": "Size",
                "value": f"{float(entry['target_size_pct_nav']) * 100:.1f}% NAV",
            })
        elig = output.get("eligibility") or {}
        if elig.get("eligible_for_plan") is not None:
            fields.append({
                "label": "Eligible",
                "value": "yes" if elig.get("eligible_for_plan") else "no",
            })
    elif agent == "risk_officer":
        fields.extend([
            {"label": "Verdict", "value": str(output.get("verdict", "—"))},
            {"label": "HITL required", "value": str(output.get("hitl_required", "—"))},
        ])
        sim = output.get("simulate_order_result") or {}
        if sim:
            fields.append({
                "label": "Sim order",
                "value": "feasible" if sim.get("feasible") else "blocked",
            })
        checks = output.get("policy_checks") or []
        if checks:
            hard = sum(1 for c in checks if c.get("hard_rule"))
            fields.append({
                "label": "Policy checks",
                "value": f"{len(checks)} total, {hard} hard",
            })
    elif agent == "plan_supervisor":
        fields.extend([
            {"label": "Verdict", "value": str(output.get("verdict", "—"))},
            {"label": "Execute?", "value": str(output.get("execution_authorized", "—"))},
            {"label": "HITL", "value": str(output.get("hitl_required", "—"))},
        ])
    elif agent == "firm_manager":
        fields.append({
            "label": "Tasks",
            "value": str(len(output.get("tasks") or [])),
        })
        sd = output.get("scan_directives") or {}
        if sd.get("narrative"):
            fields.append({"label": "Scan focus", "value": sd["narrative"][:120]})
    elif agent == "execution":
        fields.extend([
            {"label": "Status", "value": str(output.get("status", "—"))},
            {"label": "Filled qty", "value": str(output.get("filled_quantity", "—"))},
        ])
    elif agent == "idea_generator":
        fields.extend([
            {"label": "Evaluated", "value": str(output.get("candidates_evaluated", "—"))},
            {"label": "Top picks", "value": str(len(output.get("top_picks") or []))},
        ])

    return fields


def _summary_for_agent(agent: str, output: dict) -> str:
    if not output:
        return "No output recorded."
    narrative = (output.get("reasoning_narrative") or "").strip()
    if narrative:
        return narrative[:400]

    if agent == "news_triage":
        return (
            f"Decision: {output.get('decision', '—')} · "
            f"materiality {float(output.get('materiality_score') or 0):.2f}"
        )
    if agent == "fundamental_analyst":
        return (
            f"Recommended: {output.get('recommended_action', '—')} · "
            f"confidence {output.get('confidence', '—')}"
        )
    if agent == "plan_builder":
        return output.get("reasoning_narrative") or (
            f"Plan status: {output.get('status', 'drafted')}"
        )
    if agent == "risk_officer":
        return (
            f"Verdict: {output.get('verdict', '—')} · "
            f"HITL={output.get('hitl_required')} · "
            f"{(output.get('reasoning_narrative') or '')[:200]}"
        )
    if agent == "plan_supervisor":
        return (
            f"Verdict: {output.get('verdict', '—')} · "
            f"execution_authorized={output.get('execution_authorized')}"
        )
    if agent == "firm_manager":
        return (output.get("book_summary") or "")[:400]
    if agent == "execution":
        return f"Fill status: {output.get('status', '—')}"

    return json.dumps(output, default=str)[:300]


def _prepare_journal(journal_rows: list[dict], run_id: str) -> dict[str, dict]:
    """Agent name → enriched journal row (output + optional audit)."""
    audit_rows = db.audits_for_run(run_id)
    audits = {a["about_journal_id"]: a for a in audit_rows}
    by_agent: dict[str, dict] = {}
    for row in journal_rows:
        j = dict(row)
        try:
            j["output"] = json.loads(j["output_json"])
        except (json.JSONDecodeError, TypeError):
            j["output"] = {}
        audit_row = audits.get(j.get("journal_id"))
        if audit_row:
            j["audit"] = json.loads(audit_row["note_json"])
            j["audit_severity"] = audit_row.get("severity")
        agent = j.get("agent")
        if agent and agent != "auditor":
            by_agent[agent] = j
    return by_agent


def build_agent_outputs(
    state: dict,
    journal_rows: Optional[list[dict]] = None,
    run: Optional[dict] = None,
) -> list[dict[str, Any]]:
    """
    Full agent outputs for one HITL action (journal preferred, state as fallback).
    """
    run_id = (run or {}).get("run_id") or state.get("run_id", "")
    journal_by_agent: dict[str, dict] = {}
    if journal_rows and run_id:
        journal_by_agent = _prepare_journal(journal_rows, run_id)
    elif run_id:
        journal_by_agent = _prepare_journal(
            db.list_journal_for_run(run_id), run_id,
        )

    outputs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append(
        agent: str,
        state_key: str,
        label: str,
        icon: str,
    ) -> None:
        if agent in seen:
            return
        j = journal_by_agent.get(agent)
        raw = (j.get("output") if j else None)
        if state_key and raw is None:
            raw = state.get(state_key)
        if not raw:
            return
        seen.add(agent)
        entry: dict[str, Any] = {
            "agent": agent,
            "label": label,
            "icon": icon,
            "summary": _summary_for_agent(agent, raw),
            "key_fields": _key_fields_for_agent(agent, raw),
            "output": raw,
            "duration_ms": j.get("duration_ms") if j else None,
        }
        if j and j.get("audit"):
            entry["audit"] = j["audit"]
            entry["audit_severity"] = j.get("audit_severity")
        outputs.append(entry)

    for agent, state_key, label, icon in _AGENT_PIPELINE:
        _append(agent, state_key, label, icon)

    tt = (run or {}).get("trigger_type", state.get("trigger_type", ""))
    if tt == "idea_scan":
        for agent, state_key, label, icon in _EXTRA_STATE_AGENTS:
            _append(agent, state_key, label, icon)

    for agent, j in journal_by_agent.items():
        if agent in seen:
            continue
        raw = j.get("output")
        if not raw:
            continue
        seen.add(agent)
        outputs.append({
            "agent": agent,
            "label": agent.replace("_", " ").title(),
            "icon": "🤖",
            "summary": _summary_for_agent(agent, raw),
            "key_fields": _key_fields_for_agent(agent, raw),
            "output": raw,
            "duration_ms": j.get("duration_ms"),
            "audit": j.get("audit"),
            "audit_severity": j.get("audit_severity"),
        })

    return outputs


def build_hitl_brief(
    plan: dict,
    state: dict,
    run: Optional[dict] = None,
    *,
    include_agent_outputs: bool = True,
) -> dict[str, Any]:
    """Operator brief for one HITL queue item."""
    ticker = plan.get("ticker", "?")
    entry_pct = float((plan.get("entry") or {}).get("target_size_pct_nav") or 0.04)
    entry_price = float(
        (plan.get("entry") or {}).get("entry_price_or_trigger", {}).get("value") or 0
    )
    side = (plan.get("entry") or {}).get("side", "long")

    holding = _holding_for_ticker(ticker)
    if not holding:
        holding = plan_automation.holding_for_plan(plan)
    has_pos = bool(holding and int(holding.get("quantity") or 0) > 0)
    position_type = "add_on" if has_pos else "maiden"

    market = _market_panel(ticker, plan)
    timeline = _trigger_story(state, run)
    pros, cons = _pros_cons(plan, state, market, position_type)

    if position_type == "maiden":
        approval_headline = (
            f"Approve {side} entry in {ticker} — {entry_pct:.1%} NAV "
            f"@ ~${entry_price:,.2f} (new position)"
        )
    else:
        approval_headline = (
            f"Approve add-on to {ticker} — +{entry_pct:.1%} NAV "
            f"@ ~${entry_price:,.2f}"
        )

    risk = state.get("risk") or {}
    why_hitl = (
        "Maiden / new-name opening — operator sign-off required."
        if position_type == "maiden"
        else (risk.get("reasoning_narrative") or "Risk routed to HITL queue.")[:120]
    )

    pipeline = None
    run_id = (run or {}).get("run_id", "")
    journal_rows = db.list_journal_for_run(run_id) if run_id else []
    agent_outputs = (
        build_agent_outputs(state, journal_rows, run)
        if include_agent_outputs
        else []
    )

    if run:
        pipeline = pipeline_view.build_for_run(
            state,
            run.get("status", "completed"),
            trigger_type=run.get("trigger_type"),
            run_id=run.get("run_id"),
        )

    return {
        "ticker": ticker,
        "position_type": position_type,
        "position_type_label": "Add-on" if has_pos else "New position (maiden)",
        "approval_headline": approval_headline,
        "why_hitl": why_hitl,
        "timeline": timeline,
        "market": market,
        "pros": pros,
        "cons": cons,
        "entry_pct_nav": entry_pct,
        "entry_price": entry_price,
        "pipeline": pipeline,
        "agent_outputs": agent_outputs,
    }


def enrich_hitl_item(
    item: dict,
    *,
    include_agent_outputs: bool = False,
) -> dict:
    """Attach brief to a hitl_queue row (dashboard cards skip agent summaries)."""
    plan_row = db.get_plan(item["plan_id"])
    run = db.get_run(item["run_id"])
    if not plan_row:
        return {**item, "brief": None}
    plan = json.loads(plan_row["plan_json"])
    state = json.loads(run["state_json"]) if run else {}
    brief = build_hitl_brief(
        plan, state, run, include_agent_outputs=include_agent_outputs,
    )
    out: dict = {
        **item,
        "brief": brief,
        "ticker": plan.get("ticker", "?"),
    }
    if include_agent_outputs:
        out["agent_outputs"] = brief.get("agent_outputs") or []
    return out
