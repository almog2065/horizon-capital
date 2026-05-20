"""Idea Generator agent.

Proactively scans a candidate universe and ranks ideas. Complements the
reactive news-driven flow: instead of waiting for an event, this agent
asks 'what should we be researching right now?' and routes top picks
through the existing Fundamental → Plan → Risk → HITL pipeline.

Two-step output:
  1. Score & rank the candidate pool (4 dimensions: quality, valuation,
     precedent, fit).
  2. Build a STRUCTURED RESEARCH BRIEF for each top pick — business
     overview, why-now, key fundamentals, catalysts, risks, sources.

Novelty filtering: by default the agent EXCLUDES tickers we already hold
and tickers we've suggested in the past N days. This means a scan returns
genuinely new ideas to research, not a re-rank of the same names.

Decisions are grounded in:
  - fetched fundamentals (mocked, deterministic in POC)
  - dossier evidence (when present)
  - filings RAG corpus (citations)
  - past_plans RAG corpus (precedent)
  - current holdings & sector exposure (portfolio fit)
  - idea_history table (novelty)

Output is a strict JSON contract — see SYSTEM prompt.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from .. import allocation, asset_universe, config, db, llm, manager_scoring, tools
from . import firm_manager

SYSTEM = """You are the Idea Generator at Horizon Capital, a long-only long-horizon
equity firm. You do NOT trade. You produce a ranked shortlist of investment ideas
for the Fundamental Analyst to deepen, and for each top pick you write a
structured research brief.

DATA INTEGRITY RULES (read carefully):
- Each candidate carries `asset_class`, `data_provider`, `data_sources`, and
  `data_errors`. Routing (multi-asset policy §8):
  • US equities → yfinance + SEC EDGAR
  • Crypto (BTC, ETH) → CoinGecko (spot USD; no GAAP PE/FCF)
  • Commodity proxies (GLD, USO, …) → yfinance on listed ETFs
- Market data fallback chain for equities: yfinance → Yahoo chart / Stooq
  → SEC EDGAR company facts → Google News RSS. Check `data_sources` for which
  provider succeeded.
- If `data_errors` is non-empty for a candidate, ALL providers failed for that
  field. The numbers in `fundamentals_snapshot`
  are NOT real — they are zeroed placeholders. NEVER reason over them as if
  they were real, and NEVER produce a research brief that cites them as fact.
- A candidate whose fundamentals are unavailable MUST receive `recommended_action
  = "skip"` with a rationale that names the missing source. Do NOT invent
  fundamentals. Do NOT downgrade silently to "watch". Be explicit.
- A candidate whose news/filings are unavailable (but fundamentals are real)
  MAY receive "watch" with a note that evidence depth is limited.
- Refusal is acceptable. A scan that returns 0 picks because data was missing
  is more useful than one that recommends names backed by zeroed numbers.
- Crypto: do not require SEC filings; require CoinGecko quote + ≥1 news source.
  Never cite PE/FCF as equity fundamentals for BTC/ETH.
- Digital Assets aggregate ≤10% NAV; single crypto ≤5% NAV.

You favor NOVELTY: you do not re-suggest names the firm already holds or has
recently scanned, unless the user explicitly asks for them.

Inputs you receive: a candidate pool with per-name fundamentals snapshots,
filings retrieval hits, past_plan precedent hits, current holdings, sector
exposure summary, and the novelty status of each candidate (is_new, times_seen).

Output strict JSON with:
- scan_id: str
- as_of: str
- universe_size: int
- candidates_evaluated: int
- candidates_passed_screen: int
- excluded_already_held: list of tickers
- excluded_recently_suggested: list of tickers
- ranked_candidates: list of objects with keys:
    - ticker: str
    - composite_score: float in [0,1]
    - scores: {quality_score, valuation_score, precedent_score, fit_score}
    - rationale: 2-3 sentence narrative
    - portfolio_fit: {already_held, sector, sector_pct_nav, ok_to_add,
                       estimated_entry_pct_nav}
    - novelty: {is_new, times_previously_seen, last_seen_at}
    - sources: list of {source_type, ref, what_it_supports} — MIN 2 per top pick
    - recommended_action: one of
        "open_new_research" | "add_to_existing" | "watch" | "skip"
    - research_brief: present only for top picks; see schema below
- reasoning_narrative: 4-6 line summary of what the scan found
- policy_sections_cited: list of policy refs
- sources_referenced: union of source refs cited above

research_brief schema (per top pick):
{
  "executive_summary": "2-3 sentence summary",
  "business_overview": "what the company does, segments, moats",
  "why_now": "what makes this interesting today",
  "key_fundamentals": {operating_margin, revenue_growth_yoy, pe_ttm,
                       fcf_yield_pct, valuation_interpretation},
  "catalysts": {positive: [str], negative: [str]},
  "risks": [str],
  "competitive_position": "where this name sits vs peers",
  "suggested_entry": {target_size_pct_nav, entry_type, horizon},
  "recommended_research_depth": "deep_dive" | "quick_scan" | "watch",
  "sources": list of {source_type, ref, what_it_supports}
}

DECISION GUIDANCE:
- "open_new_research" → not held, composite >= 0.6, ok_to_add=true, ≥2 sources,
                       is_new OR explicitly re-evaluated
- "add_to_existing" → already held, composite >= 0.55, sector_headroom_ok, ≥2 sources
- "watch" → composite in [0.4, 0.55) OR sources < 2
- "skip" → composite < 0.4 OR not ok_to_add OR no evidence

NEW-TO-FIRM (new-name-onboarding policy §2–§3):
- `coverage_tier == new_to_firm` OR `has_dossier == false` with no prior hold:
  stricter bar — composite >= 0.65, ≥2 sources including SEC filing or discovery
  trigger, real yfinance fundamentals, market cap >= $5B. Suggested entry 3% NAV.
  `recommended_research_depth` must be `deep_dive`. Never route to plan without
  dossier + watchlist seasoning (4 weeks).

Selectivity beats coverage. A scan returning 1 high-conviction name is better
than 5 weak ones. Never invent fundamentals, dates, or quotes — every numeric
claim must trace to a tool result or RAG hit.

PORTFOLIO MANAGER DIRECTIVES (when provided) — HIGH WEIGHT:
- Manager + live book state are binding for ranking tie-breaks and open_new_research.
- Apply scan_directives.bias_sectors / deprioritize_sectors / priority_tickers /
  bias_asset_classes / deploy_urgency. Respect max_new_openings.
- When deployment_needs.active or deploy_urgency=high, favor open_new_research in
  underweight sectors and satellite sleeves (crypto, commodities, rates, FX ETFs).
- prefer_actions guides tie-breaks (e.g. add_to_existing when under-invested).
- freeze_new_entries task → no open_new_research regardless of score.
- Mechanical scores include manager_book_score; do not override manager downgrades
  without citing policy.
"""

# Thresholds aligned with data/policies/05-new-name-onboarding.md
_NEW_TO_FIRM_OPEN_SCORE = 0.65
_KNOWN_NAME_OPEN_SCORE = 0.60
# Relaxed floors when firm_state signals under-invested / under-diversified book
_DEPLOY_NEW_OPEN_SCORE = 0.52
_DEPLOY_KNOWN_OPEN_SCORE = 0.48
_DEPLOY_UNDERWEIGHT_FLOOR = 0.45
_MAIDEN_ENTRY_PCT_NAV = 0.03
_DEFAULT_ENTRY_PCT_NAV = 0.04
_MIN_MARKET_CAP_USD = 5_000_000_000


# ---------- universe loading ----------

def _load_candidate_pool() -> list[dict]:
    """Load candidates.json — falls back to dossier-only universe."""
    f = config.DATA / "candidates.json"
    if f.exists():
        try:
            data = json.loads(f.read_text())
            return list(data.get("candidate_pool") or [])
        except Exception:
            pass
    from .. import dossier_paths

    pool = []
    for ticker in dossier_paths.list_tickers():
        try:
            hit = dossier_paths.load(ticker)
            if not hit.get("found") or not hit.get("dossier"):
                continue
            d = hit["dossier"]
            pool.append({
                "ticker": d["ticker"],
                "sector": d.get("sector", "Unknown"),
                "industry": d.get("sub_industry", ""),
                "has_dossier": True,
                "blurb": d.get("business_description", "")[:200],
            })
        except Exception:
            continue
    return pool


def _enrich_candidate_coverage(c: dict) -> dict:
    """Attach firm coverage tier used by screening and onboarding policy."""
    meta = asset_universe.resolve(c["ticker"])
    cov = tools.get_firm_coverage(c["ticker"])
    c["coverage_tier"] = cov["coverage_tier"]
    c["new_to_firm"] = cov["new_to_firm"]
    c["has_dossier"] = cov["has_dossier"]
    c["firm_known"] = cov["firm_known"]
    c["asset_class"] = meta.asset_class
    c["data_provider"] = meta.data_provider
    c["sector"] = c.get("sector") or meta.sector
    return c


def _passes_universe_gate(profile: dict, ticker: str = "") -> tuple[bool, str]:
    """Investment Policy §1 + multi-asset §8."""
    if profile.get("_data_unavailable"):
        return False, "profile_unavailable"
    meta = asset_universe.resolve(ticker or profile.get("ticker", ""))
    mcap = float(profile.get("market_cap_usd") or 0)
    if meta.is_crypto:
        if mcap <= 0:
            return False, "crypto_market_cap_unavailable"
        return True, ""
    if meta.is_commodity_proxy:
        return True, ""  # listed ETF; liquidity checked at plan stage
    if mcap < _MIN_MARKET_CAP_USD:
        return False, f"market_cap_below_5b ({mcap:.0f})"
    return True, ""


def _discover_from_apis(static_pool: list[dict],
                          exclude: set[str],
                          count: int = 40) -> tuple[list[dict], dict]:
    """Multi-channel EDGAR discovery (8-K + 10-Q) for tickers outside the pool.

    Enriches each candidate with yfinance profile and tags `new_to_firm`.
    Skips names under $5B market cap or with unavailable profiles.
    """
    static_tickers = {c["ticker"] for c in static_pool}
    per_form = max(15, count // 2)
    disc = tools.discover_idea_candidates(
        count_per_form=per_form, exclude=list(exclude),
    )
    additions: list[dict] = []
    skipped: list[dict] = []
    by_ticker = disc.get("by_ticker") or {}

    if disc.get("source") == "edgar":
        for ticker, fmeta in by_ticker.items():
            if ticker in static_tickers or ticker in exclude:
                continue
            profile = tools.fetch_company_profile(ticker)
            ok, reason = _passes_universe_gate(profile, ticker=ticker)
            if not ok:
                skipped.append({"ticker": ticker, "reason": reason})
                continue
            form = fmeta.get("form", "8-K")
            additions.append(_enrich_candidate_coverage({
                "ticker": ticker,
                "sector": profile.get("sector") or "Unknown",
                "industry": profile.get("industry") or "",
                "market_cap_usd": profile.get("market_cap_usd", 0),
                "blurb": (profile.get("business_description") or "")[:200],
                "discovery_source": f"edgar_{form.lower().replace('-', '')}",
                "discovery_filing": {
                    "form": form,
                    "accession": fmeta.get("accession", ""),
                    "filing_url": fmeta.get("filing_url", ""),
                    "filed_at": fmeta.get("filed_at", ""),
                    "items": fmeta.get("items", []),
                    "company": fmeta.get("company", ""),
                    "material_event": fmeta.get("material_event", False),
                },
                "discovery_priority": fmeta.get("discovery_priority", 0),
                "_profile_source": profile.get("_source", "yfinance"),
            }))

    additions.sort(
        key=lambda a: (-(a.get("discovery_priority") or 0), a["ticker"]),
    )
    return additions, {
        "edgar_status": disc.get("source"),
        "edgar_error": disc.get("error"),
        "edgar_filings_count": len(disc.get("filings") or []),
        "edgar_channels": disc.get("channels", ["8-K"]),
        "material_8k_count": disc.get("material_8k_count", 0),
        "edgar_new_tickers": [a["ticker"] for a in additions],
        "discovery_skipped": skipped,
    }


# ---------- scoring primitives ----------

def _quality_score(fund: dict) -> float:
    if fund.get("_asset_class") == "crypto":
        mcap = float(fund.get("market_cap_usd") or 0)
        # Large-network proxy: log-scale market cap vs $1T reference
        import math
        if mcap <= 0:
            return 0.3
        raw = min(1.0, math.log10(max(mcap, 1e9)) / 12.0)
        return max(0.35, min(1.0, raw))
    op_margin = float(fund.get("operating_margin") or 0)
    growth = float(fund.get("revenue_growth_yoy") or 0)
    raw = (op_margin / 0.40) * 0.6 + (growth / 0.20) * 0.4
    return max(0.0, min(1.0, raw))


def _valuation_score(fund: dict) -> float:
    if fund.get("_asset_class") == "crypto":
        ch = abs(float(fund.get("price_change_24h_pct") or 0))
        # Lower short-term volatility → slightly higher score (heuristic)
        return max(0.25, min(0.85, 1.0 - ch / 30.0))
    pe = float(fund.get("pe_ttm") or 25)
    fcf_yield = float(fund.get("fcf_yield_pct") or 0)
    pe_term = max(0.0, min(1.0, (30 - pe) / 20))
    fcf_term = max(0.0, min(1.0, fcf_yield / 6))
    return 0.55 * pe_term + 0.45 * fcf_term


def _precedent_score(past_hits: list[dict], has_dossier: bool) -> float:
    """Dossier-backed names get a small boost; non-dossier get a baseline."""
    base = 0.20 if not has_dossier else 0.30
    if not past_hits:
        return base
    n = min(3, len(past_hits))
    return min(1.0, base + 0.15 + n * 0.18)


def _fit_score(ticker: str, sector: str,
               holdings_summary: dict,
               target_entry: float = _DEFAULT_ENTRY_PCT_NAV,
               firm_state: Optional[dict] = None,
               manager_out: Optional[dict] = None) -> tuple[float, dict]:
    if firm_state:
        return _fit_score_from_firm_state(
            ticker, sector, firm_state, target_entry, manager_out,
        )
    holdings = holdings_summary.get("holdings", [])
    sector_exp = holdings_summary.get("sector_exposures", {})
    already_held = any(h["ticker"] == ticker for h in holdings)
    sector_pct = float(sector_exp.get(sector, 0.0))
    sector_headroom = 0.25 - sector_pct
    cash_pct = float(holdings_summary.get("cash_usd", 0)) / float(
        holdings_summary.get("total_nav_usd") or 1)
    ok_to_add = (
        sector_headroom >= target_entry
        and cash_pct - target_entry >= 0.05
    )
    if already_held:
        score = 0.55 if sector_headroom > 0.08 else 0.30
    else:
        score = 0.85 if (ok_to_add and sector_headroom > 0.10) else (
            0.45 if ok_to_add else 0.15)
    return score, {
        "already_held": already_held,
        "sector": sector,
        "sector_pct_nav": sector_pct,
        "sector_headroom_pct_nav": max(0.0, sector_headroom),
        "ok_to_add": ok_to_add,
        "estimated_entry_pct_nav": target_entry,
    }


def _fit_score_from_firm_state(
    ticker: str,
    sector: str,
    firm_state: dict,
    target_entry: float,
    manager_out: Optional[dict] = None,
) -> tuple[float, dict]:
    from .. import firm_state as fs_mod

    sec = allocation.normalize_sector(sector)
    tctx = fs_mod.ticker_context(firm_state, ticker, target_entry)
    already_held = bool(tctx["held"])
    sec_row = next(
        (r for r in firm_state.get("sectors", []) if r["sector"] == sec),
        None,
    )
    sector_pct_book = float((sec_row or {}).get("pct_nav") or 0)
    band_info = allocation.analyze_sector_headroom(
        sec, sector_pct_book, target_entry,
    )
    headroom = float(
        band_info.get("headroom_to_hard_cap_pct")
        or band_info.get("headroom_pct_nav")
        or 0,
    )
    sector_pct = sector_pct_book
    band = band_info
    policy = firm_state.get("policy", {})
    float(firm_state.get("cash_pct", 0))
    invested_pct = float(firm_state.get("invested_pct", 0))
    freeze = any(
        t.get("type") == "freeze_new_entries"
        for t in (manager_out or {}).get("tasks") or []
    )
    liq = firm_state.get("liquidity") or allocation.liquidity_budget(
        float(firm_state.get("nav_usd") or 1),
        float(firm_state.get("cash_usd") or 0),
        pending_deploy_usd=float(firm_state.get("nav_usd") or 0)
        * float(firm_state.get("pending_hitl_deploy_pct_nav") or 0),
        maiden_entry=not already_held,
    )
    target_entry = allocation.cap_entry_pct_for_liquidity(
        target_entry, liq, maiden=not already_held,
    )
    ok_to_add = (
        headroom >= target_entry
        and not freeze
        and (already_held or liq.get("can_open_new_name", False))
    )
    if not already_held:
        ok_to_add = ok_to_add and target_entry > 0
    if invested_pct >= float(policy.get("max_invested_pct", 0.92)) and not already_held:
        ok_to_add = False
    deploy_needs = fs_mod.deployment_needs(firm_state)
    below_band = sector_pct_book < float(
        allocation.sector_band(sec).get("band_low_pct") or 0,
    )
    above_band = sector_pct > float(band.get("band_high_pct") or 0.25)
    posture = firm_state.get("trading_posture") or {}
    relax = bool((posture.get("knobs") or {}).get("idea_scan_relax_floors"))
    if deploy_needs.get("active") and not already_held and not freeze:
        ok_to_add = (
            headroom >= target_entry
            and liq.get("can_open_new_name", False)
        )
    if relax and posture.get("mode") in ("deploy", "diversify") and not freeze:
        if below_band and headroom >= target_entry * 0.5:
            ok_to_add = True

    if already_held:
        pos_pct = float(tctx.get("current_position_pct_nav") or 0)
        name_status = allocation.single_name_status(pos_pct)
        if name_status == "over_cap":
            score = 0.05
        elif name_status == "approaching_cap":
            score = min(0.25, 0.35 if headroom > 0.06 else 0.20)
        else:
            score = 0.70 if headroom > 0.06 else 0.35
    elif below_band and ok_to_add:
        score = 0.90
    elif ok_to_add and headroom > 0.08:
        score = 0.80
    elif ok_to_add:
        score = 0.50
    else:
        score = 0.12 if above_band else 0.20

    score = max(0.0, min(1.0, score + firm_manager.sector_score_adjustment(sec, manager_out)))
    return score, {
        "already_held": already_held,
        "sector": sec,
        "sector_pct_nav": sector_pct_book,
        "sector_headroom_pct_nav": headroom,
        "sector_target_pct": float(band.get("target_pct") or 0),
        "below_band": below_band,
        "above_band": above_band,
        "ok_to_add": ok_to_add,
        "estimated_entry_pct_nav": target_entry,
        "suggested_notional_usd": round(
            float(firm_state.get("nav_usd") or 0) * target_entry, 2,
        ),
        "deployable_cash_usd": liq.get("deployable_cash_usd"),
        "liquidity_status": liq.get("status"),
        "manager_adjusted": bool(manager_out),
    }


def _composite(scores: dict, firm_book_score: float = 0.5) -> float:
    w = max(0.0, min(0.25, float(config.MANAGER_BOOK_SCORE_WEIGHT)))
    base = 1.0 - w
    return (
        base * (
            0.28 * scores["quality_score"]
            + 0.22 * scores["valuation_score"]
            + 0.18 * scores["precedent_score"]
            + 0.32 * scores["fit_score"]
        )
        + w * firm_book_score
    )


def _has_sec_evidence(sources: list[dict], candidate_meta: dict) -> bool:
    if candidate_meta.get("discovery_filing"):
        return True
    for s in sources:
        if s.get("source_type") in (
            "sec_filing", "discovery_filing", "rag_filing",
        ):
            return True
    return False


def _has_independent_evidence(sources: list[dict], n_sources: int) -> bool:
    """§2-style evidence: SEC path OR ≥2 non-placeholder sources."""
    types = {s.get("source_type") for s in sources}
    if types & {"sec_filing", "discovery_filing", "rag_filing"}:
        return True
    real_news = sum(1 for s in sources if s.get("source_type") == "news")
    if n_sources >= 2 and real_news >= 1:
        return True
    if n_sources >= 2 and types & {"dossier", "past_plan"}:
        return True
    return False


def _evidence_sufficient(sources: list[dict], candidate_meta: dict,
                         n_sources: int, new_to_firm: bool) -> bool:
    if candidate_meta.get("asset_class") == "crypto":
        news_n = sum(1 for s in sources if s.get("source_type") == "news")
        return n_sources >= 1 and news_n >= 1
    if _has_sec_evidence(sources, candidate_meta):
        return True
    if _has_independent_evidence(sources, n_sources):
        return True
    # Scan pool names: live news + fundamentals counts when SEC is thin.
    if new_to_firm:
        news_n = sum(1 for s in sources if s.get("source_type") == "news")
        return n_sources >= 2 and news_n >= 1
    return n_sources >= 2


def _recommended_action(composite: float, fit: dict, n_sources: int,
                          is_new: bool, require_novelty: bool = True,
                          new_to_firm: bool = False,
                          has_sec_source: bool = False,
                          recently_suggested: bool = False,
                          data_ok: bool = True,
                          sources: Optional[list] = None,
                          deploy_mode: bool = False) -> str:
    if not data_ok:
        return "skip"
    if not fit["ok_to_add"]:
        return "skip" if not fit["already_held"] else "watch"
    if n_sources < 2:
        return "watch" if composite >= 0.4 else "skip"
    open_threshold = (
        _NEW_TO_FIRM_OPEN_SCORE if new_to_firm else _KNOWN_NAME_OPEN_SCORE
    )
    if deploy_mode:
        open_threshold = (
            _DEPLOY_NEW_OPEN_SCORE if new_to_firm else _DEPLOY_KNOWN_OPEN_SCORE
        )
    open_threshold += float(fit.get("open_threshold_delta") or 0)
    open_threshold = max(0.35, min(0.75, open_threshold))
    evidence_ok = _evidence_sufficient(
        sources or [], fit, n_sources, new_to_firm,
    ) or has_sec_source
    if new_to_firm and not evidence_ok:
        return "watch" if composite >= 0.5 else "skip"
    if fit["already_held"]:
        return "add_to_existing" if composite >= 0.55 else "watch"
    # Outside the novelty window → allow re-open research (not dead-end watch).
    may_open = (
        not require_novelty
        or is_new
        or not recently_suggested
    )
    if (
        deploy_mode
        and fit.get("below_band")
        and evidence_ok
        and may_open
        and composite >= _DEPLOY_UNDERWEIGHT_FLOOR
    ):
        return "open_new_research"
    if composite >= open_threshold and may_open and evidence_ok:
        return "open_new_research"
    if composite >= open_threshold:
        return "watch"
    if composite >= 0.40:
        return "watch"
    return "skip"


# ---------- research brief builder ----------

def _interpret_valuation(pe: float, fcf_yield: float) -> str:
    if pe < 15 and fcf_yield > 5:
        return "attractive — below band, high FCF yield"
    if pe < 22:
        return "within target band"
    if pe < 30:
        return "premium but defensible if quality is high"
    return "expensive — caution warranted"


def _depth_from_composite(composite: float, n_sources: int,
                          new_to_firm: bool = False) -> str:
    if new_to_firm:
        return "deep_dive" if composite >= _NEW_TO_FIRM_OPEN_SCORE and n_sources >= 2 else "watch"
    if composite >= 0.65 and n_sources >= 2:
        return "deep_dive"
    if composite >= 0.50:
        return "quick_scan"
    return "watch"


def _build_research_brief(c: dict, candidate_meta: dict,
                            dossier: Optional[dict]) -> dict:
    """Structured brief used to explain WHY this name is interesting.

    Prefers real grounded evidence (yfinance business summary, EDGAR filings,
    yfinance news) over dossier/blurb. Every fact in the brief is traceable
    to a source object in `sources`.

    If `c.data_unavailable` is true, returns a REFUSAL brief that openly
    explains the data gap rather than fabricating content.
    """
    ticker = c["ticker"]
    fund = c.get("fundamentals_snapshot") or {}
    blurb = candidate_meta.get("blurb") or ""
    edgar_hits = c.get("_edgar_filings") or []
    news_hits = c.get("_news_real") or []
    data_errors = c.get("data_errors") or []

    # Refusal brief when fundamentals are missing — honest, not fabricated
    if c.get("data_unavailable"):
        err_lines = [
            f"{e['field']}: {e['error']}"
            for e in data_errors
        ]
        return {
            "executive_summary": (
                f"{ticker}: REFUSED. Market-data sources failed for fields "
                f"[{', '.join(e['field'] for e in data_errors)}]. The Idea "
                f"Generator does not produce briefs over zeroed placeholders."
            ),
            "business_overview": (
                f"Not available — market-data lookup failed. "
                f"Errors: {'; '.join(err_lines)}"
            ),
            "why_now": (
                "No brief is produced when the required market-data sources "
                "are unavailable. Restore yfinance / EDGAR connectivity and "
                "re-run the scan."
            ),
            "key_fundamentals": {
                "operating_margin": None, "revenue_growth_yoy": None,
                "pe_ttm": None, "fcf_yield_pct": None,
                "valuation_interpretation": "unknown — data unavailable",
                "data_source": "error",
            },
            "catalysts": {"positive": [], "negative": err_lines},
            "risks": [
                "Market-data unavailable; recommendation refused.",
                "Restore connectivity before re-running scan.",
            ],
            "competitive_position": "unknown — data unavailable",
            "suggested_entry": None,
            "recommended_research_depth": "blocked_on_data",
            "data_errors": data_errors,
            "sources": c.get("sources") or [],
        }

    # Prefer real yfinance business summary, then dossier, then static blurb.
    yf_summary = fund.get("_business_summary") or ""

    if yf_summary:
        business_overview = yf_summary[:800]
    elif dossier:
        business_overview = dossier.get("business_description", blurb)
        segments = dossier.get("revenue_segments", []) or []
        if segments:
            seg_txt = "; ".join(
                f"{s.get('name')}: {int(s.get('pct_of_revenue', 0) * 100)}%"
                f" rev (yoy {int(s.get('growth_yoy', 0) * 100)}%)"
                for s in segments[:3]
            )
            business_overview += f" Segments: {seg_txt}."
    else:
        business_overview = (
            blurb or f"{ticker} — no business overview available "
            "from any source (yfinance unavailable, no dossier)."
        )

    if dossier:
        risks_seed = [
            f"{r.get('category')}: {r.get('description')}"
            for r in (dossier.get("known_risks") or [])[:3]
        ]
        peers = dossier.get("peer_set", []) or []
        competitive_position = (
            f"Peer set: {', '.join(peers)}." if peers else "Peer set not in dossier."
        )
    else:
        risks_seed = []
        if fund.get("_source") != "yfinance":
            risks_seed.append(
                "yfinance fundamentals unavailable; relying on deterministic "
                "mock — analyst must verify before plan."
            )
        if not edgar_hits:
            risks_seed.append(
                "no recent SEC filings retrieved — limit thesis confidence."
            )
        if not risks_seed:
            risks_seed.append(
                "no dossier on file; analyst should build one before plan."
            )
        competitive_position = (
            f"Sector {candidate_meta.get('sector', 'unknown')}, "
            f"industry {fund.get('_industry') or candidate_meta.get('industry', 'unknown')}. "
            "No formal peer set on file."
        )

    pe = float(fund.get("pe_ttm") or 0)
    fcf = float(fund.get("fcf_yield_pct") or 0)
    op_m = float(fund.get("operating_margin") or 0)
    growth = float(fund.get("revenue_growth_yoy") or 0)

    positives: list[str] = []
    if op_m > 0.25:
        positives.append(
            f"Operating margin {op_m:.0%} indicates pricing power."
        )
    if growth > 0.15:
        positives.append(
            f"Revenue growth {growth:.0%} YoY is well above market average."
        )
    if fcf > 4:
        positives.append(
            f"FCF yield {fcf:.1f}% supports buybacks / dividends."
        )
    if pe < 18:
        positives.append(
            f"PE {pe:.1f} below target band — valuation cushion."
        )
    if not positives:
        positives.append(
            "Composite score driven by precedent and fit rather than fundamentals."
        )

    negatives: list[str] = []
    if op_m < 0.15:
        negatives.append(
            f"Operating margin {op_m:.0%} is below firm preference (>15%)."
        )
    if growth < 0.05:
        negatives.append(
            f"Revenue growth {growth:.0%} YoY — durability questionable."
        )
    if pe > 28:
        negatives.append(
            f"PE {pe:.1f} stretched; entry timing matters."
        )
    if candidate_meta.get("has_dossier") is False:
        negatives.append(
            "No dossier on file — fundamental analyst must build one before plan."
        )

    composite = float(c.get("composite_score") or 0)
    n_sources = len(c.get("sources") or [])
    new_to_firm = bool(candidate_meta.get("new_to_firm"))
    depth = _depth_from_composite(composite, n_sources, new_to_firm=new_to_firm)
    entry_pct = (
        _MAIDEN_ENTRY_PCT_NAV if new_to_firm else _DEFAULT_ENTRY_PCT_NAV
    )

    # Why now — augment with real signals if we have them
    why_now_parts = [
        f"Screened on {time.strftime('%Y-%m-%d')}.",
    ]
    if c.get("_candidate_meta", {}).get("discovery_source") == "edgar_8k":
        d = c["_candidate_meta"].get("discovery_filing", {})
        why_now_parts.append(
            f"Surfaced from EDGAR 8-K stream "
            f"(accession {d.get('accession', '')}, "
            f"filed {d.get('filed_at', '')}) — material event filing."
        )
    if edgar_hits:
        forms = ", ".join(sorted({h.get("form") for h in edgar_hits if h.get("form")}))
        why_now_parts.append(
            f"Recent SEC filings retrieved: {forms} — analyst can cite directly."
        )
    if news_hits:
        publishers = ", ".join(sorted({n.get("publisher")
                                          for n in news_hits if n.get("publisher")})[:3])
        why_now_parts.append(
            f"{len(news_hits)} recent news items from {publishers or 'multiple sources'} "
            "available for context."
        )
    pf = c.get("portfolio_fit") or {}
    headroom = float(pf.get("sector_headroom_pct_nav") or 0)
    why_now_parts.append(
        f"Quality and valuation scores cleared the firm's selectivity bar; "
        f"{headroom:.1%} headroom under "
        f"the {pf.get('sector', 'sector')} sector cap."
    )

    # Catalysts: prefer news titles when available (real signal),
    # fall back to fundamentals-derived positives.
    news_catalysts: list[str] = []
    for n in news_hits[:3]:
        title = (n.get("title") or "").strip()
        if title:
            pub = n.get("publisher", "")
            news_catalysts.append(
                f"News: {title}" + (f" ({pub})" if pub else "")
            )
    if news_catalysts:
        positives = news_catalysts + positives

    return {
        "executive_summary": (
            f"{ticker}: composite {composite:.2f} on quality "
            f"{c['scores']['quality_score']:.2f}, valuation "
            f"{c['scores']['valuation_score']:.2f}, precedent "
            f"{c['scores']['precedent_score']:.2f}, fit "
            f"{c['scores']['fit_score']:.2f}. "
            f"Data sources: fundamentals={c.get('data_sources', {}).get('fundamentals', 'mock')}, "
            f"filings={c.get('data_sources', {}).get('edgar_filings', 'fallback')}, "
            f"news={c.get('data_sources', {}).get('news', 'fallback')}. "
            f"Recommended depth: {depth}."
        ),
        "business_overview": business_overview,
        "why_now": " ".join(why_now_parts),
        "key_fundamentals": {
            "operating_margin": op_m,
            "revenue_growth_yoy": growth,
            "pe_ttm": pe,
            "fcf_yield_pct": fcf,
            "valuation_interpretation": _interpret_valuation(pe, fcf),
            "data_source": fund.get("_source", "mock"),
        },
        "catalysts": {
            "positive": positives,
            "negative": negatives,
        },
        "risks": risks_seed,
        "competitive_position": competitive_position,
        "suggested_entry": {
            "target_size_pct_nav": entry_pct,
            "entry_type": "limit",
            "horizon": "12-18 months",
            "onboarding_tier": candidate_meta.get("coverage_tier", "unknown"),
        },
        "recommended_research_depth": depth,
        "sources": c.get("sources") or [],
    }


# ---------- main per-candidate evaluation ----------

def _evaluate_candidate(c: dict, holdings_summary: dict,
                          recently_suggested: set[str],
                          require_novelty: bool = True,
                          firm_state: Optional[dict] = None,
                          manager_out: Optional[dict] = None,
                          deploy_mode: bool = False) -> dict:
    c = _enrich_candidate_coverage(dict(c))
    ticker = c["ticker"]
    meta = asset_universe.resolve(ticker)
    sector = c.get("sector", meta.sector or "Unknown")
    new_to_firm = bool(c.get("new_to_firm"))
    target_entry = (
        _MAIDEN_ENTRY_PCT_NAV if new_to_firm else _DEFAULT_ENTRY_PCT_NAV
    )

    fund = tools.fetch_fundamentals(ticker)
    quote = tools.fetch_quote(ticker)

    # Collect data errors loudly — the LLM will see these and refuse to score
    # this candidate, not silently fold them into a 'watch' / 'open_research'.
    data_errors: list[dict] = []
    fund_src = meta.data_provider if meta.is_crypto else "yfinance"
    if fund.get("_data_unavailable") or fund.get("_source") == "error":
        data_errors.append({
            "field": "fundamentals", "source": fund_src,
            "error": fund.get("_error") or "unknown",
        })
    if quote.get("_data_unavailable") or quote.get("_source") == "error":
        data_errors.append({
            "field": "quote", "source": fund_src,
            "error": quote.get("_error") or "unknown",
        })

    # RAG evidence (curated firm corpus) — local, always available
    filings_rag = tools.search_filings(
        query=f"{ticker} business quality margins growth durability",
        ticker=ticker, top_k=2,
    )
    past = tools.search_past_plans(query=f"{ticker} thesis precedent", top_k=2)

    # Real-world evidence (EDGAR + yfinance)
    edgar_filings = tools.fetch_recent_filings_for_ticker(ticker, top_k=3)
    news_real = tools.fetch_news_for_ticker(ticker, top_k=3)
    if (
        not meta.is_crypto
        and edgar_filings.get("source") in ("error", "fallback")
        and edgar_filings.get("error")
    ):
        data_errors.append({
            "field": "edgar_filings", "source": "edgar",
            "error": edgar_filings.get("error"),
        })
    if news_real.get("source") == "error" and news_real.get("error"):
        data_errors.append({
            "field": "news", "source": "yfinance",
            "error": news_real.get("error"),
        })

    sources: list[dict] = []
    # 1) Real EDGAR filings — preferred, traceable to SEC URL
    for h in (edgar_filings.get("hits") or []):
        sources.append({
            "source_type": "sec_filing",
            "form": h.get("form"),
            "ref": h.get("ref"),
            "url": h.get("url"),
            "filed_at": h.get("filed_at"),
            "what_it_supports": h.get("what_it_supports", "material_event"),
        })
    # 2) Real news from yfinance — title + publisher + URL
    for n in (news_real.get("hits") or [])[:2]:
        sources.append({
            "source_type": "news",
            "ref": (n.get("title") or "")[:140],
            "url": n.get("url", ""),
            "publisher": n.get("publisher", ""),
            "published_at": n.get("published_at", ""),
            "what_it_supports": "recent_event",
        })
    # 3) Curated firm corpus (filings RAG) — for tickers we already track
    for h in (filings_rag.get("hits") or [])[:2]:
        sources.append({
            "source_type": "rag_filing",
            "ref": h["chunk_id"],
            "what_it_supports": "business_quality",
        })
    # 4) Past plans precedent
    for h in (past.get("hits") or [])[:1]:
        sources.append({
            "source_type": "past_plan",
            "ref": h["chunk_id"],
            "what_it_supports": "precedent",
        })
    # 5) Dossier (if present)
    if c.get("has_dossier"):
        sources.append({
            "source_type": "dossier",
            "ref": f"dossier:{ticker}",
            "what_it_supports": "business_context",
        })
    # 6) EDGAR 8-K that surfaced this candidate (if it was an API discovery)
    if c.get("discovery_filing"):
        d = c["discovery_filing"]
        sources.append({
            "source_type": "discovery_filing",
            "form": d.get("form", "8-K"),
            "ref": f"{ticker} 8-K {d.get('accession', '')}",
            "url": d.get("filing_url", ""),
            "filed_at": d.get("filed_at", ""),
            "items": d.get("items", []),
            "what_it_supports": "discovery_trigger",
        })

    qty_target = int((target_entry * (holdings_summary.get("total_nav_usd") or 1_000_000))
                     / max(quote["price"], 1))
    sim = tools.simulate_order(ticker, "long", qty_target, quote["price"])

    fit_score, fit_meta = _fit_score(
        ticker, sector, holdings_summary, target_entry=target_entry,
        firm_state=firm_state, manager_out=manager_out,
    )
    fit_meta["asset_class"] = meta.asset_class
    if not sim.get("feasible", True):
        fit_score = min(fit_score, 0.10)
        fit_meta["ok_to_add"] = False
        fit_meta["sim_violations"] = sim.get("policy_violations", [])

    # Novelty
    history = db.get_idea_history_for_ticker(ticker)
    is_new = len(history) == 0
    novelty = {
        "is_new": is_new,
        "times_previously_seen": len(history),
        "last_seen_at": (
            time.strftime("%Y-%m-%d %H:%M",
                          time.localtime(history[0]["suggested_at"]))
            if history else None
        ),
        "recently_suggested": ticker in recently_suggested,
    }

    scores = {
        "quality_score": _quality_score(fund),
        "valuation_score": _valuation_score(fund),
        "precedent_score": _precedent_score(
            past.get("hits") or [], bool(c.get("has_dossier")),
        ),
        "fit_score": fit_score,
    }
    book_score = manager_scoring.firm_book_score(firm_state, sector, ticker)
    composite = _composite(scores, firm_book_score=book_score)
    mgr_adj = manager_scoring.candidate_adjustment(
        ticker, sector, manager_out, firm_state,
        deploy_mode=deploy_mode,
        below_band=bool(fit_meta.get("below_band")),
    )
    composite = min(1.0, max(0.0, composite + mgr_adj["composite_boost"]))
    fit_score = min(1.0, max(0.0, fit_score + mgr_adj["fit_boost"]))
    fit_meta["open_threshold_delta"] = mgr_adj["open_threshold_delta"]
    fit_meta["manager_adjustment"] = mgr_adj
    fit_meta["firm_book_score"] = round(book_score, 3)
    scores["fit_score"] = fit_score
    n_sources = len(sources)
    fundamentals_missing = any(
        e["field"] in ("fundamentals", "quote") for e in data_errors
    )
    mcap = float(fund.get("market_cap_usd") or 0)
    if (
        new_to_firm
        and not meta.is_crypto
        and mcap
        and mcap < _MIN_MARKET_CAP_USD
    ):
        composite = min(composite, 0.35)
        fit_meta["ok_to_add"] = False
        fit_meta["universe_gate"] = "market_cap_below_5b"

    action = _recommended_action(
        composite, fit_meta, n_sources, is_new,
        require_novelty=require_novelty,
        new_to_firm=new_to_firm,
        has_sec_source=_has_sec_evidence(sources, c),
        recently_suggested=novelty.get("recently_suggested", False),
        data_ok=not fundamentals_missing,
        sources=sources,
        deploy_mode=deploy_mode,
    )

    # Force skip if fundamentals or quote unavailable — zeroed placeholders.
    if fundamentals_missing:
        action = "skip"
        composite = 0.0
        scores = {k: 0.0 for k in scores}

    if fundamentals_missing:
        err_fields = ", ".join(e["field"] for e in data_errors
                                  if e["field"] in ("fundamentals", "quote"))
        rationale = (
            f"{ticker}: SKIP — market data unavailable for [{err_fields}]. "
            f"Errors: {'; '.join(e['error'] for e in data_errors)[:240]}. "
            f"No real numbers — refusing to score."
        )
    else:
        rationale = (
            f"{ticker}: op margin {fund['operating_margin']:.0%}, growth "
            f"{fund['revenue_growth_yoy']:.0%}, PE {fund['pe_ttm']:.1f}, "
            f"FCF yield {fund['fcf_yield_pct']:.1f}%. "
            f"{len(past.get('hits') or [])} past-plan precedents. "
            f"Sector headroom {fit_meta['sector_headroom_pct_nav']:.1%}. "
            f"{'New name.' if is_new else f'Seen {len(history)}× before.'}"
        )
        if data_errors:
            rationale += (
                f" Data warnings: "
                f"{', '.join(e['field'] for e in data_errors)} unavailable."
            )

    return {
        "ticker": ticker,
        "composite_score": round(composite, 3),
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "rationale": rationale,
        "portfolio_fit": fit_meta,
        "novelty": novelty,
        "sources": sources,
        "recommended_action": action,
        "fundamentals_snapshot": fund,
        "current_price": quote["price"],
        "data_sources": {
            "fundamentals": fund.get("_source", "mock"),
            "quote": quote.get("_source", "mock"),
            "edgar_filings": edgar_filings.get("source", "fallback"),
            "news": news_real.get("source", "fallback"),
        },
        "data_errors": data_errors,
        "data_unavailable": fundamentals_missing,
        "asset_class": meta.asset_class,
        "data_provider": meta.data_provider,
        "firm_coverage": {
            "coverage_tier": c.get("coverage_tier"),
            "new_to_firm": new_to_firm,
            "has_dossier": c.get("has_dossier"),
        },
        "_candidate_meta": c,
        "_edgar_filings": edgar_filings.get("hits") or [],
        "_news_real": news_real.get("hits") or [],
    }


# ---------- filtering & top-pick selection ----------

def _include_held_in_scan(manager_out: Optional[dict]) -> bool:
    if not manager_out:
        return False
    prefer = (manager_out.get("scan_directives") or {}).get("prefer_actions") or []
    if "add_to_existing" in prefer:
        return True
    return any(
        t.get("type") in ("scan_add_on", "review_holding")
        for t in manager_out.get("tasks") or []
    )


def _merge_held_into_pool(
    kept: list[dict],
    full_pool: list[dict],
    firm_state: Optional[dict],
) -> list[dict]:
    """Re-include current holdings for add-on / review scoring when not in kept."""
    if not firm_state:
        return kept
    have = {c["ticker"] for c in kept}
    by_ticker = {c["ticker"]: c for c in full_pool}
    merged = list(kept)
    for p in firm_state.get("positions", []):
        t = p["ticker"]
        if t in have:
            continue
        if t in by_ticker:
            merged.append(by_ticker[t])
        else:
            merged.append({
                "ticker": t,
                "sector": p.get("sector", "Unknown"),
                "blurb": f"Current holding {t} — evaluate add-on / trim per book.",
            })
        have.add(t)
    return merged


def _filter_universe(pool: list[dict], holdings_summary: dict,
                       only_new: bool,
                       novelty_window_days: float,
                       include_held: bool = False) -> tuple[list[dict], dict]:
    """Apply held / recently-suggested filters. Returns (kept_pool, exclusion_report)."""
    from .. import config

    held_set = {h["ticker"] for h in holdings_summary.get("holdings", [])}
    recent = (
        db.recently_suggested_tickers(novelty_window_days * 24 * 3600)
        if only_new else set()
    )
    open_plans = (
        db.tickers_with_live_plan_work()
        if config.BLOCK_DUPLICATE_PIPELINE else set()
    )
    excluded_held: list[str] = []
    excluded_recent: list[str] = []
    excluded_open_plan: list[str] = []
    kept: list[dict] = []
    for c in pool:
        t = c["ticker"]
        if t in held_set and not include_held:
            excluded_held.append(t)
            continue
        if only_new and t in recent:
            excluded_recent.append(t)
            continue
        if t in open_plans:
            excluded_open_plan.append(t)
            continue
        kept.append(c)
    return kept, {
        "excluded_already_held": excluded_held,
        "excluded_recently_suggested": excluded_recent,
        "excluded_open_plan": excluded_open_plan,
        "novelty_window_days": novelty_window_days,
    }


# ---------- mock + run ----------

def _prioritize_scan_candidates(
    candidates: list[dict],
    firm_state: Optional[dict],
    manager_out: Optional[dict],
    limit: int,
) -> tuple[list[dict], int]:
    """Cap mechanical screening; prefer underweight / satellite sleeves."""
    if limit <= 0 or len(candidates) <= limit:
        return candidates, 0
    deploy_sectors: set[str] = set()
    if firm_state:
        for row in firm_state.get("sectors") or []:
            sec = allocation.normalize_sector(row.get("sector") or "")
            pct = float(row.get("pct_nav") or 0)
            band = allocation.sector_band(sec)
            if pct < float(band.get("band_low_pct") or 0):
                deploy_sectors.add(sec)
    sd = (manager_out or {}).get("scan_directives") or {}
    bias_sectors = {
        allocation.normalize_sector(r.get("sector", ""))
        for r in sd.get("bias_sectors") or []
    }
    priority = set(sd.get("priority_tickers") or [])
    satellite = {"Digital Assets", "Commodities", "Rates", "Currencies"}

    def rank_key(c: dict) -> tuple:
        t = c.get("ticker", "")
        sec = allocation.normalize_sector(c.get("sector") or "")
        ac = c.get("asset_class") or "equity"
        return (
            0 if t in priority else 1,
            0 if sec in deploy_sectors or sec in bias_sectors else 1,
            0 if sec in satellite or ac in (
                "crypto", "commodity_proxy", "rates_proxy", "fx_proxy",
            ) else 1,
            0 if c.get("discovery_source") else 1,
            t,
        )

    ordered = sorted(candidates, key=rank_key)
    return ordered[:limit], len(candidates) - limit


def _checkpoint_scan(scan_run_id: Optional[str], phase: str, **extra) -> None:
    if not scan_run_id:
        return
    from .. import graph
    graph.checkpoint_scan_progress(scan_run_id, phase, **extra)


def _mock_output(scan_id: str, as_of: str,
                  candidates: list[dict], holdings_summary: dict,
                  recently_suggested: set[str],
                  exclusion_report: dict,
                  universe_size: int, top_k: int,
                  require_novelty: bool = True,
                  discovery_meta: Optional[dict] = None,
                  firm_state: Optional[dict] = None,
                  manager_out: Optional[dict] = None,
                  deploy_mode: bool = False,
                  scan_run_id: Optional[str] = None) -> dict:
    max_eval = max(8, int(config.SCAN_MAX_EVALUATE))
    to_eval, truncated = _prioritize_scan_candidates(
        candidates, firm_state, manager_out, max_eval,
    )
    if truncated:
        exclusion_report = dict(exclusion_report)
        exclusion_report["screening_truncated"] = truncated
        exclusion_report["screening_cap"] = max_eval

    evaluated: list[dict] = []
    total = len(to_eval)
    for i, c in enumerate(to_eval):
        evaluated.append(_evaluate_candidate(
            c, holdings_summary, recently_suggested,
            require_novelty=require_novelty,
            firm_state=firm_state, manager_out=manager_out,
            deploy_mode=deploy_mode,
        ))
        if scan_run_id and (i == 0 or (i + 1) % 4 == 0 or i + 1 == total):
            _checkpoint_scan(
                scan_run_id, "screening",
                evaluated=i + 1,
                screening_total=total,
            )
    passed = [e for e in evaluated if e["recommended_action"] != "skip"]
    ranked = sorted(evaluated, key=lambda e: -e["composite_score"])

    # Top picks get a full research brief
    top_picks_full: list[dict] = []
    for e in ranked:
        if e["recommended_action"] not in ("open_new_research", "add_to_existing"):
            continue
        if len(top_picks_full) >= top_k:
            break
        dossier_res = tools.get_dossier(e["ticker"])
        dossier = dossier_res.get("dossier") if dossier_res.get("found") else None
        e["research_brief"] = _build_research_brief(e, e["_candidate_meta"], dossier)
        top_picks_full.append(e)

    # Strip internal-only keys from the output so the JSON contract is clean
    for e in ranked:
        e.pop("_candidate_meta", None)
        e.pop("_edgar_filings", None)
        e.pop("_news_real", None)

    top_summary = ", ".join(
        f"{e['ticker']}({e['composite_score']:.2f}/{e['recommended_action']})"
        for e in ranked[:5]
    )

    # Aggregate data-error report so the UI / auditor sees the failure surface
    data_errors_summary = {
        "candidates_with_errors": [
            {"ticker": e["ticker"], "errors": e.get("data_errors", [])}
            for e in ranked if e.get("data_unavailable")
        ],
        "candidates_with_partial_errors": [
            {"ticker": e["ticker"], "errors": e.get("data_errors", [])}
            for e in ranked
            if e.get("data_errors") and not e.get("data_unavailable")
        ],
        "total_evaluated": len(evaluated),
        "total_refused": sum(1 for e in ranked if e.get("data_unavailable")),
    }

    return {
        "scan_id": scan_id,
        "as_of": as_of,
        "universe_size": universe_size,
        "candidates_evaluated": len(evaluated),
        "candidates_passed_screen": len(passed),
        "excluded_already_held": exclusion_report["excluded_already_held"],
        "excluded_recently_suggested": exclusion_report["excluded_recently_suggested"],
        "novelty_window_days": exclusion_report["novelty_window_days"],
        "discovery": discovery_meta or {"enabled": False},
        "data_errors_summary": data_errors_summary,
        "ranked_candidates": ranked,
        "top_picks": top_picks_full,
        "reasoning_narrative": (
            f"Scanned a universe of {universe_size} candidates "
            f"({len(exclusion_report['excluded_already_held'])} already held, "
            f"{len(exclusion_report['excluded_recently_suggested'])} recently suggested), "
            f"evaluated {len(evaluated)}, {len(passed)} passed the selectivity "
            f"screen. Top by composite: {top_summary}. "
            f"Weighting: 30% quality, 25% valuation, 20% precedent, 25% fit."
        ),
        "policy_sections_cited": [
            "investment-policy §2", "investment-policy §3",
            "investment-policy §4", "operating-cadence §5",
            "new-name-onboarding §2", "new-name-onboarding §4",
        ],
        "sources_referenced": [s for e in ranked for s in e.get("sources", [])],
    }


_VALID_ACTIONS = frozenset(
    {"open_new_research", "add_to_existing", "watch", "skip"}
)


def _merge_mechanical_ranking(llm_ranked: list[dict],
                               mock_by_ticker: dict[str, dict]) -> list[dict]:
    """Keep LLM narrative but restore mechanical actions when LLM downgrades."""
    merged = []
    for c in llm_ranked:
        c = _normalize_candidate(c)
        m = mock_by_ticker.get(c["ticker"])
        if m:
            c.setdefault("firm_coverage", m.get("firm_coverage"))
            c.setdefault("data_sources", m.get("data_sources"))
            c.setdefault("data_errors", m.get("data_errors"))
            c.setdefault("data_unavailable", m.get("data_unavailable"))
            mock_action = m.get("recommended_action")
            llm_action = c.get("recommended_action")
            score = float(c.get("composite_score") or m.get("composite_score") or 0)
            new_to_firm = (m.get("firm_coverage") or {}).get("new_to_firm", False)
            threshold = (
                _NEW_TO_FIRM_OPEN_SCORE if new_to_firm else _KNOWN_NAME_OPEN_SCORE
            )
            if (
                mock_action in ("open_new_research", "add_to_existing")
                and llm_action == "watch"
                and score >= threshold
                and not m.get("data_unavailable")
            ):
                c["recommended_action"] = mock_action
                c["action_source"] = "mechanical_override"
            elif not c.get("composite_score"):
                c["composite_score"] = m.get("composite_score")
        merged.append(c)
    return merged


def _build_top_picks_from_ranked(
    ranked: list[dict], top_k: int,
    mock_by_ticker: Optional[dict[str, dict]] = None,
) -> list[dict]:
    """Attach research briefs to top routing candidates."""
    mock_by_ticker = mock_by_ticker or {}
    picks: list[dict] = []
    ordered = sorted(ranked, key=lambda e: -float(e.get("composite_score") or 0))
    for e in ordered:
        if e.get("recommended_action") not in ("open_new_research", "add_to_existing"):
            continue
        if len(picks) >= top_k:
            break
        m = mock_by_ticker.get(e["ticker"], {})
        meta = e.get("_candidate_meta") or m.get("_candidate_meta") or {
            "ticker": e["ticker"],
            "sector": (e.get("portfolio_fit") or {}).get("sector", "Unknown"),
            "has_dossier": (e.get("firm_coverage") or m.get("firm_coverage") or {}).get(
                "has_dossier", False,
            ),
            "new_to_firm": (e.get("firm_coverage") or m.get("firm_coverage") or {}).get(
                "new_to_firm", True,
            ),
        }
        dossier_res = tools.get_dossier(e["ticker"])
        dossier = dossier_res.get("dossier") if dossier_res.get("found") else None
        pick = dict(m) if m else dict(e)
        pick.update(e)
        for k in (
            "fundamentals_snapshot", "data_sources", "data_errors",
            "firm_coverage", "scores", "portfolio_fit", "sources",
        ):
            if k in m and (k not in e or not e.get(k)):
                pick[k] = m[k]
        pick["research_brief"] = _build_research_brief(pick, meta, dossier)
        for k in ("_candidate_meta", "_edgar_filings", "_news_real"):
            pick.pop(k, None)
        picks.append(pick)
    return picks


_ROUTING_ACTIONS = frozenset({"open_new_research", "add_to_existing"})


def build_brief_for_pick(pick: dict, dossier: Optional[dict] = None,
                         mock_row: Optional[dict] = None) -> dict:
    """Build research brief from a ranked/top pick dict."""
    if mock_row:
        for k in (
            "fundamentals_snapshot", "data_sources", "scores",
            "portfolio_fit", "firm_coverage",
        ):
            pick.setdefault(k, mock_row.get(k))
    cov = pick.get("firm_coverage") or {}
    meta = {
        "ticker": pick["ticker"],
        "sector": (pick.get("portfolio_fit") or {}).get("sector", "Unknown"),
        "has_dossier": cov.get("has_dossier", False),
        "new_to_firm": cov.get("new_to_firm", True),
        "coverage_tier": cov.get("coverage_tier"),
        "blurb": "",
    }
    return _build_research_brief(pick, meta, dossier)


def _ensure_routing_picks(ranked: list[dict], top_k: int,
                          mock_by_ticker: dict[str, dict],
                          *,
                          deploy_mode: bool = False,
                          manager_out: Optional[dict] = None) -> list[dict]:
    """Promote strong names to routing actions when LLM left top_picks empty."""
    picks = [
        e for e in ranked
        if e.get("recommended_action") in _ROUTING_ACTIONS
    ]
    cap = top_k
    if manager_out:
        sd = manager_out.get("scan_directives") or {}
        if sd.get("max_new_openings") is not None:
            cap = min(cap, int(sd["max_new_openings"]))
    if len(picks) >= cap:
        return picks[:cap]

    candidates = sorted(
        [
            e for e in ranked
            if not e.get("data_unavailable")
            and e.get("recommended_action") != "skip"
        ],
        key=lambda e: (
            -float(e.get("composite_score") or 0),
            -float((e.get("portfolio_fit") or {}).get("sector_headroom_pct_nav") or 0),
        ),
    )
    if deploy_mode:
        underweight = [
            e for e in candidates
            if (e.get("portfolio_fit") or {}).get("below_band")
        ]
        candidates = underweight + [e for e in candidates if e not in underweight]

    for e in candidates:
        if e in picks:
            continue
        m = mock_by_ticker.get(e["ticker"], {})
        new_to_firm = (m.get("firm_coverage") or e.get("firm_coverage") or {}).get(
            "new_to_firm", False,
        )
        threshold = _NEW_TO_FIRM_OPEN_SCORE if new_to_firm else _KNOWN_NAME_OPEN_SCORE
        if deploy_mode:
            threshold = (
                _DEPLOY_NEW_OPEN_SCORE if new_to_firm else _DEPLOY_KNOWN_OPEN_SCORE
            )
        score = float(e.get("composite_score") or 0)
        promote_floor = (
            _DEPLOY_UNDERWEIGHT_FLOOR
            if deploy_mode and (e.get("portfolio_fit") or {}).get("below_band")
            else threshold - 0.03
        )
        if score >= promote_floor and (e.get("portfolio_fit") or {}).get(
            "ok_to_add", True,
        ):
            e["recommended_action"] = "open_new_research"
            e["action_source"] = "scan_promotion_deploy" if deploy_mode else "scan_promotion"
            picks.append(e)
        if len(picks) >= cap:
            break
    return picks


def _normalize_candidate(c: dict) -> dict:
    action = (c.get("recommended_action") or "").lower().strip()
    if action not in _VALID_ACTIONS:
        action = {
            "open": "open_new_research", "research": "open_new_research",
            "buy": "open_new_research", "initiate": "open_new_research",
            "add": "add_to_existing", "addon": "add_to_existing",
            "monitor": "watch", "watchlist": "watch",
            "pass": "skip", "reject": "skip",
        }.get(action, "watch")
    c["recommended_action"] = action
    c.setdefault("sources", [])
    c.setdefault("portfolio_fit", {})
    c.setdefault("scores", {})
    c.setdefault("novelty", {"is_new": True, "times_previously_seen": 0})
    return c


def run(top_k: int = 3, as_of: str = "",
        candidate_pool: Optional[list[dict]] = None,
        only_new: bool = True,
        novelty_window_days: float = 7.0,
        scan_run_id: Optional[str] = None,
        enable_api_discovery: bool = True,
        api_discovery_count: Optional[int] = None,
        firm_state: Optional[dict] = None,
        manager_out: Optional[dict] = None) -> dict:
    """Scan candidates, filter to new ideas, produce ranked shortlist + briefs.

    Args:
        enable_api_discovery: if True, augment the static pool with tickers
            that just filed an 8-K on SEC EDGAR. Provides genuinely new ideas
            the firm hasn't tracked before.
        api_discovery_count: how many recent 8-Ks to scan from EDGAR.
    """
    scan_id = scan_run_id or ("scan_" + uuid.uuid4().hex[:12])
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    if api_discovery_count is None:
        api_discovery_count = int(config.SCAN_API_DISCOVERY_COUNT)
    _checkpoint_scan(scan_run_id, "discovery")

    static_pool = candidate_pool or _load_candidate_pool()
    full_pool = [_enrich_candidate_coverage(dict(c)) for c in static_pool]

    # API discovery — pull tickers from EDGAR 8-K stream
    discovery_meta: dict = {"enabled": enable_api_discovery}
    if enable_api_discovery and candidate_pool is None:
        held_set = {h["ticker"] for h in tools.get_holdings().get("holdings", [])}
        already_in_pool = {c["ticker"] for c in full_pool}
        api_excl = held_set | already_in_pool
        additions, dmeta = _discover_from_apis(
            full_pool, exclude=api_excl, count=api_discovery_count,
        )
        full_pool = full_pool + additions
        discovery_meta.update(dmeta)
        discovery_meta["static_pool_size"] = len(static_pool)
        discovery_meta["api_additions"] = len(additions)
    _checkpoint_scan(
        scan_run_id, "screening",
        universe_size=len(full_pool),
        partial_scan={"discovery": discovery_meta},
    )
    if not full_pool:
        return {
            "scan_id": scan_id, "as_of": as_of,
            "universe_size": 0,
            "candidates_evaluated": 0, "candidates_passed_screen": 0,
            "excluded_already_held": [],
            "excluded_recently_suggested": [],
            "ranked_candidates": [], "top_picks": [],
            "discovery": discovery_meta,
            "reasoning_narrative": "Empty candidate pool — nothing to evaluate.",
            "policy_sections_cited": ["operating-cadence §5"],
            "sources_referenced": [],
        }

    from .. import firm_state as fs_mod
    if not firm_state:
        firm_state = fs_mod.build_firm_state(refresh_prices=False)

    deploy = fs_mod.deployment_needs(firm_state)
    deploy_mode = bool(deploy.get("active"))
    if deploy_mode and only_new:
        only_new = False

    holdings_summary = tools.get_holdings()
    include_held = _include_held_in_scan(manager_out)
    kept_pool, exclusion_report = _filter_universe(
        full_pool, holdings_summary, only_new, novelty_window_days,
        include_held=include_held,
    )
    if include_held:
        kept_pool = _merge_held_into_pool(kept_pool, full_pool, firm_state)
        exclusion_report["included_held_for_add_on"] = list(
            firm_state.get("holdings_tickers") or [],
        )

    # If novelty filter wiped the pool, relax it once so we still produce a result.
    # IMPORTANT: preserve the original exclusion report (so the user sees what was
    # filtered) AND drop the is_new gate inside _recommended_action — otherwise
    # quality names get marked 'watch' and top_picks is empty.
    # When only_new=False, the caller explicitly opted out of novelty gating.
    novelty_relaxed = False
    require_novelty = only_new
    if not kept_pool and only_new:
        original_report = exclusion_report
        kept_pool, _relaxed_report = _filter_universe(
            full_pool, holdings_summary, only_new=False,
            novelty_window_days=novelty_window_days,
            include_held=include_held,
        )
        if include_held:
            kept_pool = _merge_held_into_pool(kept_pool, full_pool, firm_state)
        # Carry forward the originally-excluded lists for transparency
        exclusion_report = {
            "excluded_already_held": original_report["excluded_already_held"],
            "excluded_recently_suggested": original_report["excluded_recently_suggested"],
            "novelty_window_days": novelty_window_days,
        }
        novelty_relaxed = True
        require_novelty = False

    recently_set = (
        db.recently_suggested_tickers(novelty_window_days * 24 * 3600)
        if only_new else set()
    )

    mock = _mock_output(
        scan_id, as_of, kept_pool, holdings_summary, recently_set,
        exclusion_report, universe_size=len(full_pool), top_k=top_k,
        require_novelty=require_novelty,
        discovery_meta=discovery_meta,
        firm_state=firm_state,
        manager_out=manager_out,
        deploy_mode=deploy_mode,
        scan_run_id=scan_run_id,
    )
    _checkpoint_scan(scan_run_id, "ranking", evaluated=len(mock.get("ranked_candidates") or []))
    mock["firm_manager_id"] = (manager_out or {}).get("manager_id")
    mock["deployment_needs"] = deploy
    mock["deploy_mode"] = deploy_mode
    if deploy_mode:
        mock["reasoning_narrative"] = (
            f"Deploy mode: book {deploy['invested_pct']:.0%} invested "
            f"({deploy['positions_count']}/{deploy['min_position_count']} names), "
            f"cash {deploy['cash_pct']:.0%} — relaxed open thresholds for "
            f"underweight sectors. "
        ) + mock["reasoning_narrative"]
    if novelty_relaxed:
        mock["novelty_filter_relaxed"] = True
        mock["reasoning_narrative"] = (
            "Novelty filter excluded all candidates (every ticker in the "
            "universe was suggested within the novelty window), so the "
            "filter was relaxed for this scan. Recommended actions still "
            "apply, but expect duplicates with prior scans. "
        ) + mock["reasoning_narrative"]

    # Compact prompt: send only what the LLM should refine (ranking + narrative).
    # Brief construction stays local — deterministic, schema-stable.
    # CRITICAL: include data_sources + data_errors so the LLM sees failures
    # and refuses to score candidates with missing data.
    compact_cands = [
        {
            "ticker": e["ticker"],
            "sector": e["portfolio_fit"].get("sector"),
            "scores": e["scores"],
            "composite_score": e["composite_score"],
            "portfolio_fit": e["portfolio_fit"],
            "novelty": e.get("novelty", {}),
            "sources_count": len(e["sources"]),
            "data_sources": e.get("data_sources", {}),
            "data_errors": e.get("data_errors", []),
            "data_unavailable": e.get("data_unavailable", False),
            "mock_recommended_action": e["recommended_action"],
        }
        for e in mock["ranked_candidates"]
    ]
    # Aggregate scan-level error summary for the prompt header
    failed_tickers = [e["ticker"] for e in mock["ranked_candidates"]
                        if e.get("data_unavailable")]
    error_summary = ""
    if failed_tickers:
        error_summary = (
            f"\n⚠ DATA ERRORS: {len(failed_tickers)} candidate(s) had "
            f"market-data failures: {', '.join(failed_tickers)}. "
            "Their numbers are zeroed placeholders, NOT real. Per the data "
            "integrity rules, you MUST mark these as 'skip' and explain why "
            "in the rationale. Do not invent fundamentals to cover the gap.\n"
        )
    from .. import firm_state as fs_mod
    portfolio_block = fs_mod.format_for_prompt(firm_state)
    manager_block = firm_manager.format_directives_block(manager_out)

    user = (
        f"scan_id: {scan_id}\nas_of: {as_of}\ntop_k: {top_k}\n"
        f"only_new: {only_new} (window {novelty_window_days} days)\n"
        f"{error_summary}\n"
        f"Universe: {len(full_pool)} candidates total; "
        f"{len(exclusion_report.get('excluded_already_held', []))} excluded held; "
        f"{len(exclusion_report.get('excluded_recently_suggested', []))} suggested recently. "
        f"Evaluating: {len(kept_pool)}.\n\n"
        f"FIRM BOOK:\n{portfolio_block}\n\n"
        f"{manager_block}\n\n"
        f"Pre-scored candidates (ranked):\n"
        f"{json.dumps(compact_cands, indent=2)[:4500]}\n\n"
        "Refine ranking, write per-candidate rationale, assign recommended_action. "
        "REMEMBER: candidates with data_errors must be 'skip', not 'watch'. "
        "Return strict JSON per the contract."
    )

    _checkpoint_scan(scan_run_id, "llm_ranking")
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock, purpose="idea_scan")
    out.setdefault("scan_id", scan_id)
    out.setdefault("as_of", as_of)

    # If LLM returned malformed/empty ranking, fall back to mock
    if not out.get("ranked_candidates"):
        out = mock
    else:
        mock_by_ticker = {
            e["ticker"]: e for e in mock.get("ranked_candidates") or []
        }
        out["ranked_candidates"] = _merge_mechanical_ranking(
            out["ranked_candidates"], mock_by_ticker,
        )
        out.setdefault("universe_size", len(full_pool))
        out.setdefault("candidates_evaluated", len(out["ranked_candidates"]))
        out.setdefault(
            "candidates_passed_screen",
            sum(1 for c in out["ranked_candidates"]
                if c.get("recommended_action") != "skip"),
        )
        if not out.get("top_picks"):
            out["top_picks"] = mock.get("top_picks") or []
        if not out.get("top_picks"):
            out["top_picks"] = _build_top_picks_from_ranked(
                out["ranked_candidates"], top_k, mock_by_ticker,
            )
        out.setdefault(
            "excluded_already_held", mock["excluded_already_held"]
        )
        out.setdefault(
            "excluded_recently_suggested", mock["excluded_recently_suggested"]
        )
        out.setdefault("discovery", mock.get("discovery", discovery_meta))

    mock_by = {e["ticker"]: e for e in mock.get("ranked_candidates") or []}
    routing = _ensure_routing_picks(
        out.get("ranked_candidates") or [], top_k, mock_by,
        deploy_mode=deploy_mode,
        manager_out=manager_out,
    )
    if routing:
        out["top_picks"] = _build_top_picks_from_ranked(
            out["ranked_candidates"], top_k, mock_by,
        )
        if not out["top_picks"]:
            for e in routing:
                dossier_res = tools.get_dossier(e["ticker"])
                dossier = (
                    dossier_res.get("dossier")
                    if dossier_res.get("found") else None
                )
                e["research_brief"] = build_brief_for_pick(
                    e, dossier, mock_row=mock_by.get(e["ticker"]),
                )
                out.setdefault("top_picks", []).append(e)
        out["candidates_passed_screen"] = sum(
            1 for c in out["ranked_candidates"]
            if c.get("recommended_action") != "skip"
        )

    # Persist suggested tickers to idea_history so future scans can dedupe
    try:
        for pick in (out.get("top_picks") or []):
            db.record_idea_history(
                ticker=pick["ticker"],
                scan_run_id=scan_id,
                composite_score=pick.get("composite_score", 0),
                recommended_action=pick.get("recommended_action", ""),
                brief=pick.get("research_brief"),
            )
    except Exception as e:
        out["_history_record_error"] = str(e)

    return out
