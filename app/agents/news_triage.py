"""News Triage agent."""
from __future__ import annotations
import json
from .. import llm, tools

SYSTEM = """You are the firm's News Triage agent at Horizon Capital, a long-only
long-horizon equity investment firm. Your job is to decide whether a news item
deserves the firm's attention. You optimize for selectivity, not coverage.
You are not allowed to recommend trades — only score materiality.

Output strictly as a JSON object with these keys:
- decision: one of "act", "watch", "ignore"
- materiality_score: float in [0,1]
- primary_dimension: one of "fundamental","management","regulatory","macro","sentiment"
- impacted_tickers: list of ticker symbols
- is_duplicate: bool
- reasoning: short text (2-3 lines)
- policy_sections_cited: list of policy section refs
"""


def _mock_output(news, holdings_tickers, watchlist_tickers):
    """Deterministic mock: act if news mentions a held ticker, ignore otherwise."""
    # Idea-scan synthetic events always route to Fundamental (tickers may be
    # outside the dossier watchlist until onboarding completes).
    if news.get("source") in ("idea_scan_synthetic", "position_monitor"):
        tickers = list(news.get("tickers") or [])
        return {
            "decision": "act",
            "materiality_score": float(news.get("composite_score") or 0.80),
            "primary_dimension": "fundamental",
            "impacted_tickers": tickers,
            "is_duplicate": False,
            "reasoning": (
                f"Idea Generator scan routed {','.join(tickers)} to downstream "
                "research pipeline."
            ),
            "policy_sections_cited": [
                "operating-cadence §5", "new-name-onboarding §3",
            ],
        }

    in_universe = [t for t in news["tickers"]
                   if t in holdings_tickers or t in watchlist_tickers]
    if not in_universe:
        return {
            "decision": "ignore",
            "materiality_score": 0.05,
            "primary_dimension": "sentiment",
            "impacted_tickers": [],
            "is_duplicate": False,
            "reasoning": "No mentioned ticker is in firm universe.",
            "policy_sections_cited": ["operating-cadence §5"],
        }

    # Heuristic on body text — biased toward routing in-universe names to analysts
    body = (news.get("headline", "") + " " + news.get("body", "")).lower()
    severity = 0.55
    dim = "sentiment"
    if any(k in body for k in (
        "landmark", "raised guidance", "raises guidance", "beat estimates",
        "record revenue", "multi-year deal", "contract win", "rallied",
        "raised fy", "operating margin guidance was raised",
    )):
        severity = 0.88; dim = "fundamental"
    elif any(k in body for k in ("ceo", "cfo", "resign", "step down", "departure")):
        severity = 0.88; dim = "management"
    elif any(k in body for k in ("investigation", "lawsuit", "fine", "antitrust")):
        severity = 0.82; dim = "regulatory"
    elif any(k in body for k in ("guidance cut", "guidance lowered", "missed estimates",
                                  "layoff", "recall")):
        severity = 0.80; dim = "fundamental"
    elif any(k in body for k in ("guidance", "earnings", "revenue", "margin")):
        severity = 0.72; dim = "fundamental"
    elif any(k in body for k in ("acquisition", "merger", "buyback", "spinoff", "deal")):
        severity = 0.78; dim = "fundamental"
    elif any(k in body for k in ("routine", "color variant", "no other changes",
                                  "no hardware")):
        severity = 0.42; dim = "sentiment"

    decision = "act" if severity >= 0.55 else ("watch" if severity >= 0.35 else "ignore")
    return {
        "decision": decision,
        "materiality_score": severity,
        "primary_dimension": dim,
        "impacted_tickers": in_universe,
        "is_duplicate": False,
        "reasoning": f"Mentioned {','.join(in_universe)}. Heuristic dim={dim}, score={severity:.2f}.",
        "policy_sections_cited": ["operating-cadence §5", "investment-policy §7"],
    }


def run(news: dict, holdings_tickers: list[str],
        watchlist_tickers: list[str],
        firm_state: dict | None = None) -> dict:
    portfolio_block = ""
    if firm_state:
        from .. import firm_state as fs_mod
        tickers = news.get("tickers") or []
        t = tickers[0] if tickers else None
        portfolio_block = (
            f"\nFIRM BOOK:\n{fs_mod.format_for_prompt(firm_state, ticker=t)}\n"
        )
    user = (
        f"News item:\n{json.dumps(news, indent=2)}\n\n"
        f"Firm holdings tickers: {holdings_tickers}\n"
        f"Firm watchlist tickers: {watchlist_tickers}\n"
        f"{portfolio_block}\n"
        "Score materiality vs current book and policy. Held names with "
        "thesis-breaking news should score higher. Return strict JSON."
    )
    mock = _mock_output(news, holdings_tickers, watchlist_tickers)
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock)
    # Ensure required keys
    out.setdefault("news_id", news.get("id", ""))
    out.setdefault("retrieved_evidence", [])

    # Coerce LLM variants
    _VALID_DECISIONS = {"act", "watch", "ignore"}
    _ALIASES = {
        "actionable": "act", "material": "act", "high": "act", "trigger": "act",
        "monitor": "watch", "medium": "watch",
        "low": "ignore", "no_action": "ignore", "skip": "ignore", "drop": "ignore",
    }
    decision = (out.get("decision") or "").lower().strip()
    if decision not in _VALID_DECISIONS:
        out["decision"] = _ALIASES.get(decision, "ignore")

    # Fallback by score if decision still inconsistent
    score = out.get("materiality_score", 0) or 0
    try:
        score = float(score)
    except Exception:
        score = 0
    if out["decision"] == "ignore" and score >= 0.55:
        out["decision"] = "act"
    if out["decision"] == "watch" and score >= 0.72:
        out["decision"] = "act"
    if out["decision"] == "act" and score < 0.35:
        out["decision"] = "ignore"

    out.setdefault("recommended_next_agent",
                   "fundamental_analyst" if out.get("decision") == "act" else None)

    # Post-LLM guard: never drop scan/monitor pipeline events at triage.
    if news.get("source") in ("idea_scan_synthetic", "position_monitor"):
        out["decision"] = "act"
        out["impacted_tickers"] = list(news.get("tickers") or [])
        out["materiality_score"] = max(
            float(out.get("materiality_score") or 0),
            float(news.get("composite_score") or 0.75),
        )
        out["recommended_next_agent"] = "fundamental_analyst"

    return out
