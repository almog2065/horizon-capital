"""Metrics computed by the eval harness.

Pure functions over plain dicts so they're trivially testable and
re-runnable on archived trace dumps.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class PortfolioMetrics:
    starting_nav: float
    ending_nav: float
    pnl_absolute: float
    pnl_pct: float
    benchmark_pct: float
    excess_return_pct: float
    max_drawdown_pct: float
    hit_rate: float
    n_trades: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CostMetrics:
    n_llm_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_usd: float
    by_purpose: list[dict]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ProcessMetrics:
    n_agent_calls: int
    n_llm_calls: int
    grounded_calls: int          # LLM calls that included citations
    grounded_ratio: float
    citations_per_decision: float
    refusal_count: int
    hitl_required: int
    hitl_resolved: int
    guardrail_checks: int
    guardrail_passed: int
    guardrail_breaches: int
    guardrail_effectiveness: float  # passed / checks (1.0 if no checks)
    hitl_discipline: float          # resolved / required (1.0 if none required)
    decision_quality: float         # composite process score 0–1

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# -----------------------------------------------------------------------------
# Pure helpers
# -----------------------------------------------------------------------------
def max_drawdown(equity_curve: Iterable[float]) -> float:
    """Return the largest peak-to-trough percentage drop in the equity curve."""
    peak = float("-inf")
    worst = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            worst = max(worst, dd)
    return worst


def hit_rate(trades: list[dict]) -> float:
    """Fraction of closed trades with non-negative realized PnL."""
    closed = [t for t in trades if t.get("realized_pnl") is not None]
    if not closed:
        return 0.0
    wins = sum(1 for t in closed if (t.get("realized_pnl") or 0) >= 0)
    return wins / len(closed)


def compute_portfolio(
    starting_nav: float,
    equity_curve: list[float],
    benchmark_pct: float,
    trades: list[dict],
) -> PortfolioMetrics:
    ending = equity_curve[-1] if equity_curve else starting_nav
    pnl = ending - starting_nav
    pnl_pct = (pnl / starting_nav) * 100.0 if starting_nav else 0.0
    return PortfolioMetrics(
        starting_nav=starting_nav,
        ending_nav=ending,
        pnl_absolute=pnl,
        pnl_pct=pnl_pct,
        benchmark_pct=benchmark_pct,
        excess_return_pct=pnl_pct - benchmark_pct,
        max_drawdown_pct=max_drawdown(equity_curve) * 100.0,
        hit_rate=hit_rate(trades) * 100.0,
        n_trades=len(trades),
    )


def compute_cost(traces: list[dict]) -> CostMetrics:
    """Estimate LLM spend from replay or live trace shapes."""
    try:
        from app.model_routing import estimate_cost_usd, model_for
    except ImportError:
        return CostMetrics(0, 0, 0, 0.0, [])

    buckets: dict[str, dict] = {}
    prompt_tok = 0
    completion_tok = 0
    total_usd = 0.0
    n_llm = 0

    for t in traces:
        if t.get("kind") != "llm_call":
            continue
        n_llm += 1
        purpose = str(t.get("purpose") or "unknown")
        model = str(t.get("model") or model_for(purpose))
        tok = t.get("tokens") or {}
        p = int(t.get("prompt_tokens") or tok.get("prompt") or 800)
        c = int(t.get("completion_tokens") or tok.get("completion") or 400)
        if t.get("estimated_cost_usd") is not None:
            usd = float(t["estimated_cost_usd"])
        else:
            usd = estimate_cost_usd(model, p, c)
        prompt_tok += p
        completion_tok += c
        total_usd += usd
        b = buckets.setdefault(purpose, {"purpose": purpose, "calls": 0, "tokens": 0, "usd": 0.0, "model": model})
        b["calls"] += 1
        b["tokens"] += p + c
        b["usd"] += usd

    by_purpose = sorted(buckets.values(), key=lambda x: -x["usd"])
    return CostMetrics(
        n_llm_calls=n_llm,
        prompt_tokens=prompt_tok,
        completion_tokens=completion_tok,
        total_usd=round(total_usd, 4),
        by_purpose=by_purpose,
    )


def compute_decision_quality(
    *,
    grounded_ratio: float,
    hitl_discipline: float,
    guardrail_effectiveness: float,
    approval_rate: float,
) -> float:
    """Composite process score (brief: decision quality + groundedness + guardrails)."""
    return round(
        0.40 * grounded_ratio
        + 0.25 * hitl_discipline
        + 0.20 * guardrail_effectiveness
        + 0.15 * approval_rate,
        4,
    )


def compute_process(traces: list[dict]) -> ProcessMetrics:
    n_agent = sum(1 for t in traces if t.get("kind") == "agent_call")
    n_llm = sum(1 for t in traces if t.get("kind") == "llm_call")
    grounded = sum(
        1 for t in traces
        if t.get("kind") == "llm_call" and (t.get("citations") or []) != []
    )
    refusals = sum(1 for t in traces if t.get("outcome") == "refused")
    hitl_req = sum(1 for t in traces if t.get("event") == "hitl_required")
    hitl_res = sum(1 for t in traces if t.get("event") == "hitl_resolved")
    checks = [t for t in traces if t.get("event") == "guardrail_check"]
    passed = sum(1 for t in checks if t.get("passed"))
    breaches = sum(1 for t in traces if t.get("event") == "guardrail_breach")

    citations_total = sum(len(t.get("citations") or []) for t in traces if t.get("kind") == "llm_call")
    cpd = (citations_total / n_llm) if n_llm else 0.0
    grounded_ratio = (grounded / n_llm) if n_llm else 0.0
    guard_eff = (passed / len(checks)) if checks else 1.0
    hitl_disc = (hitl_res / hitl_req) if hitl_req else 1.0
    blocked = sum(1 for t in traces if t.get("outcome") == "blocked")
    approval_rate = 1.0 - ((refusals + blocked) / n_agent) if n_agent else 1.0
    approval_rate = max(0.0, min(1.0, approval_rate))
    if n_llm == 0 and n_agent == 0:
        dq = 0.0
    else:
        dq = compute_decision_quality(
            grounded_ratio=grounded_ratio,
            hitl_discipline=hitl_disc,
            guardrail_effectiveness=guard_eff,
            approval_rate=approval_rate,
        )

    return ProcessMetrics(
        n_agent_calls=n_agent,
        n_llm_calls=n_llm,
        grounded_calls=grounded,
        grounded_ratio=grounded_ratio,
        citations_per_decision=cpd,
        refusal_count=refusals,
        hitl_required=hitl_req,
        hitl_resolved=hitl_res,
        guardrail_checks=len(checks),
        guardrail_passed=passed,
        guardrail_breaches=breaches,
        guardrail_effectiveness=round(guard_eff, 4),
        hitl_discipline=round(hitl_disc, 4),
        decision_quality=dq,
    )
