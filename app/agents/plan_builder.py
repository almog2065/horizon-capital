"""Plan Builder agent."""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from .. import db, llm, tools
from ..portfolio import safe_float

SYSTEM = """You are the Plan Builder at Horizon Capital. You construct trading plans
that live for months. A plan defines thesis, entry, monitoring rules, guardrails, and exit.

A plan can be built when ALL mechanical gates pass:
- fundamental_read.recommended_action in ("eligible_for_plan", "flag_for_hitl", "propose_thesis_review")
- equities: market cap >= $5B; crypto: CoinGecko market cap > 0 (no GAAP); commodity ETFs: listed
- simulate_order.feasible == true
- no earnings in next 14 days (equities only — N/A for crypto per multi-asset policy §8)
- post-entry sector exposure ≤ 25%
- new-name-onboarding: no maiden plan without dossier on file (§3 stage 3)
- capital-allocation §3–§5: post-entry sector ≤ 25% NAV; respect cash floor; size to
  sector headroom when firm_state is provided

CRITICAL: "propose_thesis_review" and "flag_for_hitl" mean draft a plan and send to HITL —
they are NOT reasons for status=not_eligible. Only reject when a mechanical gate fails
(wrong action enum, market cap too small, sim_order infeasible, earnings blackout, sector cap).
Output strict JSON with: status, plan_id?, plan?, reasoning_narrative,
precedent_summary, pre_check_results, policy_sections_cited, sources_referenced,
next_step.
"""


_PLAN_FUNDAMENTAL_ACTIONS = frozenset({
    "eligible_for_plan", "flag_for_hitl", "propose_thesis_review",
})


def _mechanical_gates_pass(
    ticker: str,
    fundamental: dict,
    sim: dict,
    firm_state: dict | None = None,
) -> tuple[bool, dict]:
    from .. import asset_universe
    meta = asset_universe.resolve(ticker)
    dossier_res = tools.get_dossier(ticker)
    coverage = tools.get_firm_coverage(ticker)
    mcap = 0.0
    if dossier_res.get("found"):
        mcap = float((dossier_res.get("dossier") or {}).get("market_cap_usd") or 0)
    if mcap < 5e9:
        mcap = float(fundamental.get("market_cap_usd") or 0)
    if meta.is_crypto:
        mcap_ok = mcap > 0 and not fundamental.get("_data_unavailable")
    elif meta.is_commodity_proxy or meta.is_rates_proxy or meta.is_fx_proxy:
        mcap_ok = mcap >= 1e8 or not fundamental.get("_data_unavailable")
    else:
        mcap_ok = mcap >= 5e9
    held = coverage.get("currently_held", False)
    maiden = not held
    dossier = dossier_res.get("dossier") or {}
    scan_onboarded = (
        dossier.get("onboarding_source") == "idea_scan"
        and dossier.get("watchlist_seasoned") is True
    )
    checks = {
        "fundamental_action_ok": fundamental.get("recommended_action") in _PLAN_FUNDAMENTAL_ACTIONS,
        "market_cap_ok": mcap_ok,
        "asset_class": meta.asset_class,
        "sim_order_feasible": bool(sim.get("feasible")),
        "dossier_on_file": bool(dossier_res.get("found")),
        "maiden_position": maiden,
        "new_name_dossier_gate": (
            (not maiden)
            or (dossier_res.get("found") and (
                scan_onboarded or not dossier.get("onboarding_source")
            ))
        ),
        "scan_onboarded": scan_onboarded,
        "market_cap_usd": mcap,
        "coverage_tier": coverage.get("coverage_tier"),
        "fundamental_read_recommended_action": fundamental.get("recommended_action"),
    }
    if firm_state:
        from .. import firm_state as fs_mod
        entry_pct = _entry_pct_nav(ticker, fundamental)
        tctx = fs_mod.ticker_context(firm_state, ticker, entry_pct)
        checks["sector_headroom_ok"] = tctx["sector_headroom"].get("within_hard_cap", True)
        checks["portfolio_fit"] = tctx
    else:
        checks["sector_headroom_ok"] = True
    return all(checks[k] for k in (
        "fundamental_action_ok", "market_cap_ok", "sim_order_feasible",
        "new_name_dossier_gate", "sector_headroom_ok",
    )), checks


def _entry_pct_nav(ticker: str, fundamental: dict) -> float:
    dossier_res = tools.get_dossier(ticker)
    if dossier_res.get("found"):
        d = dossier_res["dossier"]
        if d.get("suggested_entry_pct_nav"):
            return float(d["suggested_entry_pct_nav"])
        if d.get("onboarding_source") == "idea_scan":
            return 0.03
    if fundamental.get("scan_context"):
        return 0.03
    return 0.04


def _valuation_target_range(fundamental: dict) -> dict[str, Any]:
    """Normalize fundamental valuation band (LLM may return N/A or a string)."""
    val = fundamental.get("valuation") or {}
    tr = val.get("target_range")
    low, high = 0.0, 0.0
    if isinstance(tr, (list, tuple)):
        low = safe_float(tr[0] if len(tr) > 0 else None)
        high = safe_float(tr[1] if len(tr) > 1 else None)
    elif isinstance(tr, str):
        parts = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", tr.replace(",", ""))
        if len(parts) >= 2:
            low, high = float(parts[0]), float(parts[1])
        elif len(parts) == 1:
            high = float(parts[0])
    metric = str(val.get("primary_metric") or "p_e")
    return {"metric": metric, "low": low, "high": high}


def _build_plan_dict(ticker: str, fundamental: dict, past_hits: list[dict],
                     quote: dict, sim: dict, as_of: str) -> dict:
    plan_id = "plan_" + uuid.uuid4().hex[:12]
    entry_price = quote["price"]
    target_size_pct = _entry_pct_nav(ticker, fundamental)

    plan = {
        "id": plan_id,
        "ticker": ticker,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "draft",
        "thesis": {
            "narrative": fundamental.get("reasoning_narrative", ""),
            "supporting_points": [
                fundamental.get("business_quality", {}).get("summary", "Business quality"),
                f"Valuation interpretation: {fundamental.get('valuation', {}).get('interpretation','')}",
                f"Durability: {fundamental.get('business_quality', {}).get('durability','')}",
            ],
            "valuation_target_range": _valuation_target_range(fundamental),
            "expected_holding_horizon": "12-18 months",
        },
        "entry": {
            "side": "long",
            "target_size_pct_nav": target_size_pct,
            "entry_type": "limit",
            "entry_price_or_trigger": {"type": "limit_price", "value": entry_price},
            "execution_window_days": 5,
        },
        "monitoring": {
            "interval": "daily",
            "checks": [
                {"name": "price_drift", "type": "price_drift",
                 "threshold_pct": -0.25, "on_breach": "trigger_re_eval"},
                {"name": "news_materiality", "type": "news_materiality",
                 "threshold": 0.7, "on_breach": "trigger_re_eval"},
                {"name": "filing_relevance", "type": "filing_relevance",
                 "threshold": "any_8K_or_10Q", "on_breach": "flag"},
                {"name": "earnings_proximity", "type": "event_proximity",
                 "threshold_days": 7, "on_breach": "log"},
            ],
        },
        "guardrails": {
            "soft_stop_loss_pct": -0.25,
            "hard_position_cap_pct_nav": 0.08,
            "time_stop": {"max_holding_period_months": 24, "action": "review"},
            "breach_actions": {
                "thesis_violation": "review",
                "valuation_overshoot": "trim",
                "management_change": "review",
                "earnings_disappointment": "review",
            },
        },
        "exit": {
            "target_realized": {
                "action": "trim_half",
                "conditions": ["valuation > 1.5x target_high"],
            },
            "thesis_break": {
                "action": "exit",
                "conditions": ["≥2 supporting_points invalidated"],
            },
        },
        "history": [{
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": "plan_builder",
            "action": "created",
            "data_snapshot": {"price": entry_price, "as_of": as_of},
        }],
        "past_similar_plan_refs": [h["chunk_id"] for h in past_hits[:3]],
    }
    return plan


def _mock_output(ticker: str, fundamental: dict, past_hits: list[dict],
                 quote: dict, sim: dict, as_of: str) -> dict:
    # Gate checks — flag_for_hitl and thesis_review still produce a draft for HITL
    eligible = fundamental.get("recommended_action") in (
        "eligible_for_plan", "flag_for_hitl", "propose_thesis_review",
    )
    if not eligible:
        return {
            "status": "not_eligible",
            "plan_id": None,
            "plan": None,
            "reasoning_narrative": (
                f"Fundamental recommended_action is "
                f"'{fundamental.get('recommended_action')}', not eligible_for_plan."
            ),
            "precedent_summary": "",
            "pre_check_results": {"fundamental_eligible": False},
            "policy_sections_cited": ["investment-policy §4"],
            "sources_referenced": fundamental.get("sources_referenced", []),
            "next_step": "abandon",
        }

    if not sim["feasible"]:
        return {
            "status": "not_eligible",
            "plan_id": None,
            "plan": None,
            "reasoning_narrative": (
                f"Simulated order infeasible: "
                f"{[v['reason'] for v in sim['policy_violations']]}"
            ),
            "precedent_summary": "",
            "pre_check_results": {"sim_order_feasible": False},
            "policy_sections_cited": [v["policy_section"]
                                       for v in sim["policy_violations"]],
            "sources_referenced": fundamental.get("sources_referenced", []),
            "next_step": "abandon",
        }

    plan = _build_plan_dict(ticker, fundamental, past_hits, quote, sim, as_of)

    precedent_summary = (
        f"Found {len(past_hits)} past plans in similar setup. "
        f"Used top {min(3, len(past_hits))} as references."
    )

    return {
        "status": "drafted",
        "plan_id": plan["id"],
        "plan": plan,
        "reasoning_narrative": (
            f"Drafted plan {plan['id']} for {ticker}. "
            f"Entry 4% NAV at limit price {quote['price']:.2f}. "
            f"Daily monitoring with thesis-aware breach actions. "
            f"Awaiting HITL approval."
        ),
        "precedent_summary": precedent_summary,
        "pre_check_results": {
            "market_cap_ok": True,
            "fundamental_eligible": True,
            "sim_order_feasible": True,
            "sector_headroom_ok": True,
        },
        "policy_sections_cited": ["investment-policy §2", "investment-policy §3",
                                   "investment-policy §4"],
        "sources_referenced": fundamental.get("sources_referenced", []),
        "next_step": "send_to_hitl",
    }


def run(ticker: str, fundamental: dict, as_of: str = "",
        firm_state: dict | None = None) -> dict:
    quote = tools.fetch_quote(ticker)

    # Honest refusal: if market data is unavailable, don't draft a plan over
    # placeholder prices. Bail out with status=not_eligible + clear reason.
    if (quote.get("_data_unavailable") or quote.get("_source") == "error"
            or fundamental.get("_data_unavailable")):
        err = quote.get("_error") or fundamental.get("_error") or "data unavailable"
        return {
            "status": "not_eligible",
            "plan_id": None,
            "plan": None,
            "reasoning_narrative": (
                f"Plan draft refused for {ticker}: market data unavailable "
                f"({err}). Plan Builder will not size positions over placeholder "
                f"prices."
            ),
            "precedent_summary": "",
            "pre_check_results": {"data_available": False},
            "policy_sections_cited": ["risk-policy §5"],
            "sources_referenced": [],
            "next_step": "abandon",
            "_data_unavailable": True,
        }

    price = quote["price"] or 1.0  # defensive — should be real at this point
    if not firm_state:
        firm_state = tools.get_firm_state(refresh_prices=False)
    from .. import firm_state as fs_mod
    entry_pct = _entry_pct_nav(ticker, fundamental)
    tctx = fs_mod.ticker_context(firm_state, ticker, entry_pct)
    headroom = float(tctx["sector_headroom"].get("headroom_to_hard_cap_pct") or entry_pct)
    entry_pct = min(entry_pct, headroom) if headroom > 0 else entry_pct
    liq = firm_state.get("liquidity") or {}
    maiden = not tctx.get("held")
    from .. import allocation
    entry_pct = allocation.cap_entry_pct_for_liquidity(
        entry_pct, liq, maiden=maiden,
    )
    if maiden and not liq.get("can_open_new_name", True):
        return {
            "status": "not_eligible",
            "plan_id": None,
            "plan": None,
            "reasoning_narrative": (
                f"Plan draft refused for {ticker}: deployable cash "
                f"${liq.get('deployable_cash_usd', 0):,.0f} insufficient for a new "
                f"name while preserving {liq.get('reserve_pct', 0.08):.0%} NAV cash reserve."
            ),
            "precedent_summary": "",
            "pre_check_results": {"liquidity_ok": False, "liquidity": liq},
            "policy_sections_cited": ["capital-allocation §1"],
            "sources_referenced": [],
            "next_step": "abandon",
        }
    nav = float(firm_state.get("nav_usd") or 1_000_000)
    quantity_target = int((entry_pct * nav) / price)
    sim = tools.simulate_order(ticker, "long", quantity_target, price)
    past = tools.search_past_plans(query=fundamental.get("reasoning_narrative", "")[:300],
                                    top_k=5)
    portfolio_block = fs_mod.format_for_prompt(firm_state, ticker, entry_pct)

    user = (
        f"Ticker: {ticker}\nAs of: {as_of}\n\n"
        f"FIRM PORTFOLIO:\n{portfolio_block}\n\n"
        f"Fundamental read:\n{json.dumps(fundamental, indent=2)[:2500]}\n\n"
        f"Current quote: {json.dumps(quote)}\n"
        f"Simulate order result: {json.dumps(sim)}\n\n"
        f"Past plans hits: {json.dumps(past.get('hits', []), indent=2)[:1500]}\n\n"
        "Produce a plan draft result as strict JSON."
    )
    mock = _mock_output(ticker, fundamental, past.get("hits", []), quote, sim, as_of)
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock)
    out.setdefault("status", "insufficient_data")

    gates_ok, gate_checks = _mechanical_gates_pass(
        ticker, fundamental, sim, firm_state,
    )
    if gates_ok and out.get("status") != "drafted":
        # Live LLM often mis-reads propose_thesis_review as not_eligible — trust mechanical gates
        out = mock
        out["pre_check_results"] = {
            **(out.get("pre_check_results") or {}),
            **gate_checks,
            "llm_status_overridden": True,
        }

    # If LLM returned a plan dict but no plan_id, ensure consistency
    if out.get("status") == "drafted":
        plan = out.get("plan")
        if not plan:
            # Use the mock-built plan to keep system functional
            out = mock
        else:
            plan.setdefault("entry", {})["target_size_pct_nav"] = entry_pct
            db.save_plan(plan["id"], plan["ticker"], "draft", plan)
            out["plan_id"] = plan["id"]
    return out
