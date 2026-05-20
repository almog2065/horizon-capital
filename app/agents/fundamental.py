"""Fundamental Analyst agent."""
from __future__ import annotations
import json
from .. import llm, tools

SYSTEM = """You are the Fundamental Analyst at Horizon Capital, a long-only
long-horizon investment firm. You evaluate businesses, not stocks. Every claim
must be traceable to a source the operator can audit.

Return strict JSON with keys:
- business_quality: { summary, durability, margin_trend, key_metrics: [] }
- management: { summary, recent_changes, concerns }
- valuation: { primary_metric, current_value, target_range, interpretation }
- catalysts: { positive: [...], negative: [...] }
- risks: { new: [...], elevated: [...] }
- thesis_strength: one of "strong","moderate","weak","no_thesis"
- thesis_intact: one of "intact","weakened","broken","unclear" (or null)
- recommended_action: one of "no_action","move_to_watchlist","eligible_for_plan","flag_for_hitl","propose_thesis_review"
- reasoning_narrative: 5-10 line executive summary
- sources_referenced: list of {source_type, ref, what_it_supports} — MIN 3
- policy_sections_cited: list of policy section refs

DECISION GUIDANCE for recommended_action:
- "eligible_for_plan" → bullish catalyst + valuation in/below target range + intact thesis on quality business. Plan Builder will draft a trade.
- "flag_for_hitl" → uncertain but actionable; Risk Officer will require operator HITL before execution.
- "move_to_watchlist" → interesting but needs more time/data. Stops the flow.
- "propose_thesis_review" → event_triggered_review on a held position whose thesis is weakened/broken.
- "no_action" → no thesis or universe-disqualifying.

When a clearly bullish material catalyst (large contract win, raised guidance,
strong earnings beat) is presented on a quality business in the firm's universe,
the correct action is "eligible_for_plan" or "flag_for_hitl", not "move_to_watchlist".

PORTFOLIO AWARENESS (capital-allocation policy §5–§6): You receive the live firm
portfolio snapshot. Factor cash %, sector weights vs strategic targets, whether
the name is already held, and sector headroom before recommending entries. Prefer
underweight sectors; if the name's sector is near the 25% hard cap, use
flag_for_hitl or move_to_watchlist rather than eligible_for_plan. Mention portfolio
fit in reasoning_narrative.

MULTI-ASSET (policy §8): For `asset_class=crypto` (BTC, ETH) use CoinGecko market
metrics — do not apply GAAP PE/FCF/earnings framing. For commodity ETFs (GLD, USO)
treat as listed vehicles with underlying exposure. Digital Assets aggregate ≤10% NAV;
single crypto ≤5% NAV.
"""


def _news_text(context: dict | None) -> str:
    if not context:
        return ""
    news = context.get("news") or {}
    return f"{news.get('headline', '')} {news.get('body', '')}".lower()


def _mock_output(ticker: str, mode: str, dossier_found: bool, fund: dict,
                 filings_hits: list[dict], past_hits: list[dict],
                 context: dict | None = None) -> dict:
    """Deterministic mock with realistic-looking content."""
    sources = []
    for h in filings_hits[:2]:
        sources.append({
            "source_type": "filing",
            "ref": h["chunk_id"],
            "what_it_supports": "fundamental_view",
        })
    for h in past_hits[:1]:
        sources.append({
            "source_type": "past_plan",
            "ref": h["chunk_id"],
            "what_it_supports": "precedent",
        })
    if not sources:
        sources = [{"source_type": "dossier", "ref": f"dossier:{ticker}",
                    "what_it_supports": "business_context"}]

    pe = fund.get("pe_ttm", 20)
    op_margin = fund.get("operating_margin", 0.2)
    growth = fund.get("revenue_growth_yoy", 0.1)

    margin_trend = "expanding" if op_margin > 0.25 else (
        "stable" if op_margin > 0.15 else "compressing")
    durability = "high" if growth > 0.15 else ("medium" if growth > 0.05 else "low")

    # Valuation: target band is hash-driven
    target_low = pe * 0.85
    target_high = pe * 1.15
    valuation_interp = "within"

    text = _news_text(context)
    bullish = any(k in text for k in (
        "landmark", "raised guidance", "raises guidance", "beat estimates",
        "record revenue", "multi-year deal", "contract win", "rallied",
        "raised fy", "operating margin guidance was raised", "buyback",
    ))
    negative = any(k in text for k in (
        "investigation", "lawsuit", "guidance cut", "guidance lowered",
        "missed estimates", "layoff",
    ))

    if mode == "event_triggered_review":
        if bullish:
            thesis_intact = "intact"
            recommended = "eligible_for_plan"
            thesis_strength = "strong" if dossier_found else "moderate"
        elif negative:
            thesis_intact = "weakened"
            recommended = "flag_for_hitl"
            thesis_strength = None
        else:
            # Management / mixed events on held names — route to plan, not dead-end review
            thesis_intact = "weakened" if any(k in text for k in (
                "resign", "step down", "departure", "cfo", "ceo",
            )) else "unclear"
            recommended = "flag_for_hitl"
            thesis_strength = "moderate" if dossier_found else None
    else:
        thesis_intact = None
        thesis_strength = "moderate" if dossier_found else "weak"
        mgmt_change = any(k in text for k in (
            "resign", "step down", "departure", "cfo", "ceo",
        ))
        if mgmt_change and not bullish:
            recommended = "flag_for_hitl"
        elif bullish and dossier_found:
            recommended = "eligible_for_plan"
        elif thesis_strength in ("strong", "moderate") and len(sources) >= 2:
            recommended = "eligible_for_plan"
        else:
            recommended = "flag_for_hitl" if dossier_found else "move_to_watchlist"

    return {
        "ticker": ticker,
        "invocation_mode": mode,
        "as_of": "",  # filled by caller
        "business_quality": {
            "summary": f"{ticker} shows {durability} growth durability "
                       f"with {margin_trend} margin trend.",
            "durability": durability,
            "margin_trend": margin_trend,
            "key_metrics": [
                {"name": "operating_margin", "current": op_margin,
                 "prior_year": op_margin - 0.01,
                 "source_ref": sources[0]["ref"] if sources else ""},
                {"name": "revenue_growth_yoy", "current": growth,
                 "prior_year": growth - 0.02,
                 "source_ref": sources[0]["ref"] if sources else ""},
            ],
        },
        "management": {
            "summary": "Long-tenured leadership, no recent disruption.",
            "recent_changes": [],
            "concerns": [],
        },
        "valuation": {
            "primary_metric": "p_e",
            "current_value": pe,
            "target_range": [target_low, target_high],
            "interpretation": valuation_interp,
        },
        "catalysts": {
            "positive": [{"description": "Capital return continued", "expected_window": "ongoing", "confidence": "medium"}],
            "negative": [{"description": "Macro slowdown risk", "expected_window": "6-12m", "confidence": "low"}],
        },
        "risks": {
            "new": [],
            "elevated": [],
        },
        "thesis_strength": thesis_strength,
        "thesis_intact": thesis_intact,
        "recommended_action": recommended,
        "reasoning_narrative": (
            f"{ticker} has a {durability} business profile with {margin_trend} margins. "
            f"Valuation at P/E {pe:.1f} sits {valuation_interp} our target band. "
            f"No fresh material risks; thesis appears {'moderate' if mode != 'event_triggered_review' else 'weakened by event'}. "
            f"Recommended action: {recommended}."
        ),
        "sources_referenced": sources,
        "policy_sections_cited": ["investment-policy §4", "investment-policy §1"],
    }


def run(ticker: str, mode: str = "new_research", as_of: str = "",
        context: dict | None = None) -> dict:
    dossier_res = tools.get_dossier(ticker)
    coverage = tools.get_firm_coverage(ticker)
    fund = tools.fetch_fundamentals(ticker)

    # Honest refusal: if the underlying market-data source failed, don't
    # produce a thesis over zeroed placeholders. Escalate to HITL with a
    # clear reason so the operator can either retry or override.
    if fund.get("_data_unavailable") or fund.get("_source") == "error":
        return {
            "ticker": ticker,
            "invocation_mode": mode,
            "as_of": as_of,
            "business_quality": {"summary": "data unavailable", "durability": "unknown",
                                   "margin_trend": "unknown", "key_metrics": []},
            "management": {"summary": "data unavailable", "recent_changes": [],
                            "concerns": []},
            "valuation": {"primary_metric": "p_e", "current_value": None,
                            "target_range": [None, None],
                            "interpretation": "unknown"},
            "catalysts": {"positive": [], "negative": []},
            "risks": {"new": [{"category": "data_integrity",
                                  "description": fund.get("_error", "fundamentals unavailable")}],
                       "elevated": []},
            "thesis_strength": None,
            "thesis_intact": None,
            "recommended_action": "flag_for_hitl",
            "reasoning_narrative": (
                f"{ticker}: fundamentals fetch failed — "
                f"{fund.get('_error', 'unknown error')}. "
                "Refusing to invent numbers. Escalating to HITL for operator "
                "judgment (retry data source or override)."
            ),
            "sources_referenced": [
                {"source_type": "error", "ref": "yfinance",
                  "what_it_supports": "data_unavailable"},
            ],
            "policy_sections_cited": ["risk-policy §5", "investment-policy §4"],
            "_data_unavailable": True,
        }

    filings = tools.search_filings(query=f"{ticker} thesis fundamentals", ticker=ticker,
                                    top_k=3)
    past = tools.search_past_plans(query=f"{ticker} {mode}", top_k=3)

    firm_state = (context or {}).get("firm_state")
    if not firm_state:
        firm_state = tools.get_firm_state(refresh_prices=False)
    from .. import firm_state as fs_mod, trading_posture
    portfolio_block = fs_mod.format_for_prompt(firm_state, ticker=ticker)
    posture_block = trading_posture.format_posture_block(
        firm_state.get("trading_posture") or trading_posture.derive_posture(firm_state),
    )

    user = (
        f"Ticker: {ticker}\nInvocation mode: {mode}\nAs of: {as_of}\n\n"
        f"FIRM PORTFOLIO (decide in this context):\n{portfolio_block}\n\n"
        f"{posture_block}\n\n"
        f"Firm coverage: {json.dumps(coverage, indent=2)[:800]}\n\n"
        f"Dossier found: {dossier_res.get('found')}\n"
        f"Dossier (excerpt): {json.dumps(dossier_res, indent=2)[:1500]}\n\n"
        f"Fetched fundamentals: {json.dumps(fund, indent=2)}\n\n"
        f"Filings retrieval ({len(filings.get('hits', []))} hits):\n"
        f"{json.dumps(filings.get('hits', []), indent=2)[:1500]}\n\n"
        f"Past plans retrieval ({len(past.get('hits', []))} hits):\n"
        f"{json.dumps(past.get('hits', []), indent=2)[:1500]}\n\n"
        "Produce a fundamental read as strict JSON."
    )
    mock = _mock_output(ticker, mode, dossier_res.get("found", False), fund,
                        filings.get("hits", []), past.get("hits", []), context)
    mock["as_of"] = as_of
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock)
    out.setdefault("ticker", ticker)
    out.setdefault("invocation_mode", mode)
    out.setdefault("as_of", as_of)
    out.setdefault("recommended_action", "move_to_watchlist")

    # Coerce LLM variants to our enum
    _VALID_ACTIONS = {"no_action", "move_to_watchlist", "eligible_for_plan",
                      "flag_for_hitl", "propose_thesis_review"}
    _ALIASES = {
        "buy": "eligible_for_plan",
        "open_position": "eligible_for_plan",
        "open": "eligible_for_plan",
        "initiate": "eligible_for_plan",
        "watchlist": "move_to_watchlist",
        "watch": "move_to_watchlist",
        "monitor": "move_to_watchlist",
        "hitl": "flag_for_hitl",
        "escalate": "flag_for_hitl",
        "hold": "no_action",
        "pass": "no_action",
        "review": "propose_thesis_review",
    }
    action = (out.get("recommended_action") or "").lower().strip()
    if action not in _VALID_ACTIONS:
        out["recommended_action"] = _ALIASES.get(action, "move_to_watchlist")

    # Watchlist sweep / constructive new_research: route to plan, not dead-end review
    news = (context or {}).get("news") or {}
    text = _news_text(context)
    sweep = news.get("source") in (
        "watchlist_sweep", "idea_scan_synthetic", "position_monitor",
    )
    constructive = sweep or any(k in text for k in (
        "constructive setup", "valuation in range", "watchlist sweep",
        "new_research eligibility", "eligible_for_plan",
        "idea generator discovery", "open_new_research",
        "position monitor", "event-triggered review",
    ))
    if constructive:
        composite = float(
            news.get("composite_score")
            or (news.get("scan_pick") or {}).get("composite_score")
            or 0
        )
        if dossier_res.get("found"):
            if out["recommended_action"] in (
                "propose_thesis_review", "move_to_watchlist", "no_action",
            ):
                out["recommended_action"] = "eligible_for_plan"
        elif out["recommended_action"] in (
            "move_to_watchlist", "no_action",
        ):
            # Scan pick without dossier → research path ends at HITL, not silent stop.
            out["recommended_action"] = "flag_for_hitl"
        if composite >= 0.58 and out["recommended_action"] == "flag_for_hitl":
            out["recommended_action"] = "eligible_for_plan"
            out["scan_routing"] = "high_conviction_scan_pick"

    out["market_cap_usd"] = float(fund.get("market_cap_usd") or 0)
    out["portfolio_fit"] = fs_mod.ticker_context(firm_state, ticker)
    if not out["portfolio_fit"]["sector_headroom"].get("within_hard_cap"):
        if out["recommended_action"] == "eligible_for_plan":
            out["recommended_action"] = "flag_for_hitl"
            out["portfolio_routing"] = "sector_near_cap"
    if news.get("scan_pick"):
        out["scan_context"] = {
            "scan_run_id": news.get("scan_run_id"),
            "composite_score": news.get("composite_score"),
            "recommended_action": (news.get("scan_pick") or {}).get(
                "recommended_action",
            ),
        }

    return out
