"""Position Monitor agent.

Evaluates open holdings against the active plan's monitoring.checks,
guardrails, and exit rules. Uses live market data (quote, news, filings)
and optionally LLM synthesis. Does not auto-trade — breaches route to
trigger_re_eval (agent pipeline), flag (operator), or log.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from .. import config, llm, tools
from ..portfolio import safe_float
from . import news_triage

SYSTEM = """You are the Position Monitor at Horizon Capital. You oversee OPEN
positions against their active trading plan (monitoring, guardrails, exit).

You receive mechanical check results (already computed from market data).
Your job: synthesize status, prioritize breaches, and recommend next steps
consistent with the plan's on_breach / breach_actions fields.

RULES:
- YOU decide whether operator HITL is needed via hitl_required (boolean on output).
- Default for routine monitoring on an open position: hitl_required=false.
- Set hitl_required=true for thesis break, full exit, or material governance/regulatory events.
- price_drift / soft_stop breach → usually trigger_re_eval with hitl_required=false unless
  loss breaches risk-policy §2 thesis-break thresholds.
- news_materiality >= threshold → trigger_re_eval; escalate hitl_required only if material.
- filing_relevance → flag or trigger_re_eval; hitl_required if thesis-breaking.
- trim_hint: set hitl_required=false only for routine drift trim within plan; true for large trims.
- If all checks pass: overall_status=healthy, recommended_actions=[], hitl_required=false.

Output strict JSON:
- ticker, plan_id, as_of
- market_snapshot: {price, entry_price, return_pct, pct_nav, days_held, ...}
- checks: list of mechanical check results (pass through, may add narrative)
- guardrail_results: list
- breaches: list of {check_name, severity, detail, planned_action}
- overall_status: healthy | attention | action_required
- hitl_required: bool — your judgment whether operator must approve before any trade
- recommended_actions: list of {action, check_name, rationale, hitl_required?}
  action one of: log, flag, trigger_re_eval, review, trim_hint
- reasoning_narrative: 4-8 lines
- policy_sections_cited: list
"""


def _entry_price(plan: dict, holding: dict) -> float:
    entry = (plan.get("entry") or {}).get("entry_price_or_trigger") or {}
    v = float(entry.get("value") or 0)
    if v > 0:
        return v
    return float(holding.get("cost_basis") or holding.get("current_price") or 1)


def _horizon_days(plan: dict) -> int:
    h = (plan.get("thesis") or {}).get("expected_holding_horizon") or "12 months"
    months = [int(x) for x in re.findall(r"\d+", h)]
    if len(months) >= 2:
        return int((months[0] + months[1]) / 2 * 30.44)
    if months:
        return int(months[0] * 30.44)
    return 365


def _plan_created_ts(plan: dict) -> float:
    created = plan.get("created_at") or ""
    try:
        return time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return time.time() - 90 * 86400


def _check_price_drift(
    chk: dict, entry: float, current: float,
) -> dict:
    threshold = float(chk.get("threshold_pct", -0.25))
    ret = (current - entry) / entry if entry else 0.0
    breached = ret <= threshold
    return {
        "name": chk.get("name", "price_drift"),
        "type": "price_drift",
        "status": "breach" if breached else "ok",
        "breached": breached,
        "value": round(ret, 4),
        "threshold": threshold,
        "on_breach": chk.get("on_breach", "trigger_re_eval"),
        "detail": (
            f"Return {ret:.1%} vs entry {entry:.2f} "
            f"(threshold {threshold:.0%})."
        ),
    }


def _check_news_materiality(
    chk: dict, ticker: str, holdings: list[str], watchlist: list[str],
) -> dict:
    threshold = float(chk.get("threshold", 0.7))
    news = tools.fetch_news_for_ticker(ticker, top_k=3)
    hits = news.get("hits") or []
    if not hits:
        return {
            "name": chk.get("name", "news_materiality"),
            "type": "news_materiality",
            "status": "ok",
            "breached": False,
            "value": 0.0,
            "threshold": threshold,
            "on_breach": chk.get("on_breach", "trigger_re_eval"),
            "detail": "No recent news retrieved.",
        }
    headline = hits[0].get("title") or f"{ticker} news"
    body = hits[0].get("title") or ""
    synthetic = {
        "id": f"mon_news_{ticker}_{int(time.time())}",
        "headline": headline,
        "body": body,
        "tickers": [ticker],
        "source": "position_monitor",
    }
    triage = news_triage.run(synthetic, holdings, watchlist)
    score = float(triage.get("materiality_score") or 0)
    breached = score >= threshold and triage.get("decision") == "act"
    return {
        "name": chk.get("name", "news_materiality"),
        "type": "news_materiality",
        "status": "breach" if breached else "ok",
        "breached": breached,
        "value": score,
        "threshold": threshold,
        "on_breach": chk.get("on_breach", "trigger_re_eval"),
        "detail": (
            f"Triage materiality {score:.2f} on '{headline[:80]}' "
            f"(decision={triage.get('decision')})."
        ),
        "triage": triage,
    }


def _check_filing_relevance(chk: dict, ticker: str) -> dict:
    filings = tools.fetch_recent_filings_for_ticker(ticker, top_k=3)
    hits = filings.get("hits") or []
    forms = [h.get("form") for h in hits if h.get("form")]
    relevant = [f for f in forms if f in ("8-K", "10-Q", "10-K")]
    breached = len(relevant) > 0
    return {
        "name": chk.get("name", "filing_relevance"),
        "type": "filing_relevance",
        "status": "breach" if breached else "ok",
        "breached": breached,
        "value": relevant,
        "threshold": chk.get("threshold", "any_8K_or_10Q"),
        "on_breach": chk.get("on_breach", "flag"),
        "detail": (
            f"Recent SEC: {', '.join(relevant) or 'none'}."
            if relevant else "No recent 8-K/10-Q in retrieval window."
        ),
    }


def _check_earnings_proximity(chk: dict, ticker: str) -> dict:
    """POC: deterministic days-to-earnings stub from ticker hash."""
    h = sum(ord(c) for c in ticker) % 45
    days_until = h + 3
    threshold = int(chk.get("threshold_days", 7))
    breached = days_until <= threshold
    return {
        "name": chk.get("name", "earnings_proximity"),
        "type": "event_proximity",
        "status": "breach" if breached else "ok",
        "breached": breached,
        "value": days_until,
        "threshold": threshold,
        "on_breach": chk.get("on_breach", "log"),
        "detail": f"Estimated {days_until}d to next earnings (demo estimator).",
    }


def _evaluate_guardrails(
    plan: dict, holding: dict, entry: float, current: float, nav: float,
) -> list[dict]:
    g = plan.get("guardrails") or {}
    results: list[dict] = []
    ret = (current - entry) / entry if entry else 0.0
    qty = int(holding.get("quantity") or 0)
    mv = qty * current
    pct_nav = mv / nav if nav else 0.0
    days_held = max(0, int((time.time() - _plan_created_ts(plan)) / 86400))

    soft = float(g.get("soft_stop_loss_pct", -0.25))
    if ret <= soft:
        action = (g.get("breach_actions") or {}).get("thesis_violation", "review")
        results.append({
            "name": "soft_stop_loss",
            "breached": True,
            "value": ret,
            "threshold": soft,
            "planned_action": action,
            "detail": f"Return {ret:.1%} at/below soft stop {soft:.0%}.",
        })

    cap = float(g.get("hard_position_cap_pct_nav", 0.08))
    if pct_nav > cap:
        results.append({
            "name": "hard_position_cap",
            "breached": True,
            "value": pct_nav,
            "threshold": cap,
            "planned_action": "trim",
            "detail": f"Position {pct_nav:.1%} NAV exceeds cap {cap:.0%}.",
        })

    ts = g.get("time_stop") or {}
    max_m = int(ts.get("max_holding_period_months", 24))
    max_days = max_m * 30
    if days_held >= max_days:
        results.append({
            "name": "time_stop",
            "breached": True,
            "value": days_held,
            "threshold": max_days,
            "planned_action": ts.get("action", "review"),
            "detail": f"Held {days_held}d ≥ max {max_days}d.",
        })

    # Valuation overshoot vs plan band
    band = (plan.get("thesis") or {}).get("valuation_target_range") or {}
    high = safe_float(band.get("high"))
    if high > 0 and current > high * 1.5:
        results.append({
            "name": "valuation_overshoot",
            "breached": True,
            "value": current,
            "threshold": high * 1.5,
            "planned_action": (g.get("breach_actions") or {}).get(
                "valuation_overshoot", "trim",
            ),
            "detail": f"Price {current:.2f} > 1.5× band high {high:.2f}.",
        })

    return results


def _breaches_to_actions(
    checks: list[dict], guardrails: list[dict],
) -> list[dict]:
    actions: list[dict] = []
    seen: set[str] = set()
    for c in checks:
        if not c.get("breached"):
            continue
        ob = c.get("on_breach", "log")
        key = f"{c['name']}:{ob}"
        if key in seen:
            continue
        seen.add(key)
        act = ob
        if ob == "trigger_re_eval":
            act = "trigger_re_eval"
        elif ob == "flag":
            act = "flag"
        else:
            act = "log"
        actions.append({
            "action": act,
            "check_name": c["name"],
            "rationale": c.get("detail", ""),
        })
    for g in guardrails:
        if not g.get("breached"):
            continue
        pa = g.get("planned_action", "review")
        if pa == "trim":
            act = "trim_hint"
        elif pa == "review":
            act = "trigger_re_eval"
        else:
            act = "flag"
        key = f"guardrail:{g['name']}:{act}"
        if key in seen:
            continue
        seen.add(key)
        actions.append({
            "action": act,
            "check_name": g["name"],
            "rationale": g.get("detail", ""),
        })
    return actions


def _mechanical_eval(
    ticker: str,
    holding: dict,
    plan: dict,
    holdings_tickers: list[str],
    watchlist_tickers: list[str],
) -> dict:
    quote = tools.fetch_quote(ticker)
    current = float(quote.get("price") or holding.get("current_price") or 0)
    entry = _entry_price(plan, holding)
    nav = float(config.STARTING_NAV)
    qty = int(holding.get("quantity") or 0)
    mv = qty * current
    pct_nav = mv / nav if nav else 0.0
    ret = (current - entry) / entry if entry else 0.0
    days_held = max(0, int((time.time() - _plan_created_ts(plan)) / 86400))

    checks_cfg = (plan.get("monitoring") or {}).get("checks") or []
    checks_out: list[dict] = []
    for chk in checks_cfg:
        ctype = chk.get("type") or chk.get("name")
        if ctype == "price_drift":
            checks_out.append(_check_price_drift(chk, entry, current))
        elif ctype == "news_materiality":
            checks_out.append(_check_news_materiality(
                chk, ticker, holdings_tickers, watchlist_tickers,
            ))
        elif ctype == "filing_relevance":
            checks_out.append(_check_filing_relevance(chk, ticker))
        elif ctype in ("event_proximity", "earnings_proximity"):
            checks_out.append(_check_earnings_proximity(chk, ticker))
        else:
            checks_out.append({
                "name": chk.get("name", ctype),
                "type": ctype,
                "status": "skipped",
                "breached": False,
                "detail": "Unknown check type.",
            })

    guardrails = _evaluate_guardrails(plan, holding, entry, current, nav)
    breaches = [
        {
            "check_name": c["name"],
            "severity": "high" if c["type"] == "price_drift" else "medium",
            "detail": c.get("detail"),
            "planned_action": c.get("on_breach"),
        }
        for c in checks_out if c.get("breached")
    ]
    for g in guardrails:
        breaches.append({
            "check_name": g["name"],
            "severity": "high" if g["name"] == "hard_position_cap" else "medium",
            "detail": g.get("detail"),
            "planned_action": g.get("planned_action"),
        })

    actions = _breaches_to_actions(checks_out, guardrails)
    n_breach = len(breaches)
    if n_breach == 0:
        overall = "healthy"
    elif any(a["action"] == "trigger_re_eval" for a in actions):
        overall = "action_required"
    else:
        overall = "attention"

    hitl_required = any(
        a.get("hitl_required") for a in actions if a.get("hitl_required") is not None
    )
    if not hitl_required and overall == "action_required":
        hitl_required = any(
            b.get("severity") == "high" for b in breaches
        ) and ret <= -0.10

    return {
        "ticker": ticker,
        "plan_id": plan.get("id") or holding.get("plan_id"),
        "market_snapshot": {
            "price": current,
            "entry_price": entry,
            "return_pct": round(ret, 4),
            "pct_nav": round(pct_nav, 4),
            "market_value_usd": round(mv, 2),
            "days_held": days_held,
            "horizon_days": _horizon_days(plan),
            "quote_source": quote.get("_source", "mock"),
        },
        "checks": checks_out,
        "guardrail_results": guardrails,
        "breaches": breaches,
        "overall_status": overall,
        "hitl_required": hitl_required,
        "recommended_actions": actions,
        "reasoning_narrative": (
            f"{ticker}: {len(checks_out)} monitoring checks, "
            f"{len(guardrails)} guardrail signals, {n_breach} breach(es). "
            f"Return {ret:.1%}, {pct_nav:.1%} NAV, held {days_held}d."
        ),
        "policy_sections_cited": [
            "investment-policy §5", "investment-policy §6",
            "operating-cadence §1", "operating-cadence §5",
        ],
    }


def run(
    ticker: str,
    holding: dict,
    plan: dict,
    as_of: str = "",
    holdings_tickers: Optional[list[str]] = None,
    watchlist_tickers: Optional[list[str]] = None,
    firm_state: Optional[dict] = None,
    manager_out: Optional[dict] = None,
) -> dict:
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    holdings_tickers = holdings_tickers or []
    watchlist_tickers = watchlist_tickers or []

    mock = _mechanical_eval(
        ticker, holding, plan, holdings_tickers, watchlist_tickers,
    )
    mock["as_of"] = as_of

    portfolio_block = ""
    manager_block = ""
    if firm_state:
        from .. import firm_state as fs_mod
        portfolio_block = f"FIRM BOOK:\n{fs_mod.format_for_prompt(firm_state, ticker)}\n\n"
    if manager_out:
        from . import firm_manager
        manager_block = firm_manager.format_directives_block(manager_out) + "\n\n"

    user = (
        f"Ticker: {ticker}\nPlan id: {plan.get('id')}\nAs of: {as_of}\n\n"
        f"{portfolio_block}{manager_block}"
        f"Mechanical evaluation:\n{json.dumps(mock, indent=2)[:6000]}\n\n"
        "Refine overall_status and recommended_actions using book context. "
        "Return strict JSON per contract."
    )
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock, purpose="position_monitor")
    out.setdefault("ticker", ticker)
    out.setdefault("plan_id", plan.get("id"))
    out.setdefault("as_of", as_of)
    if not out.get("checks"):
        out["checks"] = mock["checks"]
    if not out.get("guardrail_results"):
        out["guardrail_results"] = mock["guardrail_results"]
    if not out.get("market_snapshot"):
        out["market_snapshot"] = mock["market_snapshot"]
    if not out.get("recommended_actions") and mock.get("recommended_actions"):
        out["recommended_actions"] = mock["recommended_actions"]
    if not out.get("breaches") and mock.get("breaches"):
        out["breaches"] = mock["breaches"]
    out.setdefault("overall_status", mock["overall_status"])
    if out.get("hitl_required") is None:
        out["hitl_required"] = mock.get("hitl_required", False)
    return out
