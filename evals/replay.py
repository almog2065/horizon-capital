"""Historical replay engine.

Reads a windowed scenario (``evals/data/<window>.json``) and replays trades
deterministically. Records equity curve, trades, and trace events that mirror
``app/traces.py`` shapes for process-quality metrics.

Deterministic when ``USE_MOCK_LLM=1`` (default in CI).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

from .benchmark import resolve_benchmark
from .guardrails import check_trade

log = get_logger("horizon.eval.replay")


@dataclass
class ReplayResult:
    starting_nav: float
    equity_curve: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    traces: list[dict] = field(default_factory=list)
    benchmark_pct: float = 0.0
    benchmark_symbol: str = "SPY"
    benchmark_source: str = "scenario"
    window: str = "sample"


def load_scenario(window: str) -> dict:
    here = Path(__file__).resolve().parent / "data"
    candidate = here / f"{window}.json"
    if not candidate.exists():
        candidate = here / "sample.json"
    if not candidate.exists():
        return _builtin_sample()
    with candidate.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _builtin_sample() -> dict:
    """Tiny inline scenario so CI never breaks on missing fixtures."""
    return {
        "window": "sample",
        "starting_nav": 1_000_000.0,
        "benchmark_symbol": "SPY",
        "benchmark_pct": 0.45,
        "events": [
            {
                "ticker": "MSFT",
                "action": "buy",
                "qty": 100,
                "price": 410.0,
                "citations": ["10K-2024:p27", "EDGAR:MSFT-2025-Q1"],
                "hitl": False,
            },
            {
                "ticker": "NVDA",
                "action": "buy",
                "qty": 50,
                "price": 880.0,
                "citations": ["news:nvda-guidance-2025-05-15"],
                "hitl": True,
            },
            {
                "ticker": "MSFT",
                "action": "sell",
                "qty": 100,
                "price": 415.5,
                "realized_pnl": 550.0,
                "citations": ["plan:msft-2025"],
                "hitl": False,
            },
        ],
    }


def _append_llm_trace(
    traces: list[dict],
    ev: dict,
    *,
    ticker: str,
    grounded: bool,
) -> None:
    citations = list(ev.get("citations") or [])
    traces.append({
        "kind": "llm_call",
        "agent": "fundamental",
        "ticker": ticker,
        "purpose": "thesis_check",
        "citations": citations,
        "model": "gpt-4o",
        "tokens": {"prompt": 2400, "completion": 900, "total": 3300},
        "prompt_tokens": 2400,
        "completion_tokens": 900,
    })
    if not grounded:
        traces.append({
            "kind": "agent_call",
            "agent": "risk_officer",
            "ticker": ticker,
            "event": "decision",
            "outcome": "refused",
        })


def replay(window: str = "sample") -> ReplayResult:
    """Run historical replay for ``window``; deterministic for a fixed scenario."""
    scenario = load_scenario(window)
    starting = float(scenario["starting_nav"])
    bench_meta = resolve_benchmark(scenario)
    bench = float(bench_meta["return_pct"])

    cash = starting
    holdings: dict[str, dict[str, float]] = {}
    equity: list[float] = [starting]
    trades: list[dict] = []
    traces: list[dict] = []
    last_px: dict[str, float] = {}

    for ev in scenario["events"]:
        ticker = ev["ticker"]
        action = ev["action"]
        qty = float(ev["qty"])
        px = float(ev["price"])
        last_px[ticker] = px
        hitl_required = bool(ev.get("hitl"))
        citations = ev.get("citations") or []
        grounded = bool(citations) or not ev.get("require_citations", True)

        nav_before = cash + sum(
            h["qty"] * last_px.get(t, h["avg_cost"])
            for t, h in holdings.items()
            if h["qty"] > 0
        )

        decision_idx = len(traces)
        traces.append({
            "kind": "agent_call",
            "agent": "plan_supervisor",
            "ticker": ticker,
            "event": "decision",
            "outcome": "pending",
        })
        _append_llm_trace(traces, ev, ticker=ticker, grounded=grounded)

        if hitl_required:
            traces.append({"kind": "event", "event": "hitl_required", "ticker": ticker})
            traces.append({"kind": "event", "event": "hitl_resolved", "ticker": ticker})

        ok, violations = check_trade(ev, nav=nav_before, holdings=holdings)
        traces.append({
            "kind": "event",
            "event": "guardrail_check",
            "ticker": ticker,
            "passed": ok,
            "violations": violations,
        })
        if not ok:
            traces.append({
                "kind": "event",
                "event": "guardrail_breach",
                "ticker": ticker,
                "violations": violations,
            })
            traces[decision_idx]["outcome"] = "blocked"
            equity.append(nav_before)
            continue

        if not grounded:
            traces[decision_idx]["outcome"] = "refused"
            equity.append(nav_before)
            continue

        traces[decision_idx]["outcome"] = "approved"

        if action == "buy":
            cost = qty * px
            cash -= cost
            pos = holdings.get(ticker, {"qty": 0.0, "avg_cost": px})
            pos["qty"] = pos.get("qty", 0.0) + qty
            pos["avg_cost"] = px
            holdings[ticker] = pos
            trades.append({"ticker": ticker, "side": "buy", "qty": qty, "price": px})
        elif action == "sell":
            pos = holdings.get(ticker, {"qty": 0.0, "avg_cost": px})
            proceeds = qty * px
            cash += proceeds
            realized = ev.get("realized_pnl")
            if realized is None and pos["qty"]:
                realized = (px - pos["avg_cost"]) * qty
            pos["qty"] -= qty
            holdings[ticker] = pos
            trades.append({
                "ticker": ticker,
                "side": "sell",
                "qty": qty,
                "price": px,
                "realized_pnl": realized,
            })

        nav = cash + sum(
            h["qty"] * last_px.get(t, h["avg_cost"])
            for t, h in holdings.items()
            if h["qty"] > 0
        )
        equity.append(nav)

    log.info("replay-done events=%d equity_final=%.2f", len(scenario["events"]), equity[-1])
    return ReplayResult(
        starting_nav=starting,
        equity_curve=equity,
        trades=trades,
        traces=traces,
        benchmark_pct=bench,
        benchmark_symbol=str(bench_meta["symbol"]),
        benchmark_source=str(bench_meta["source"]),
        window=str(scenario.get("window", window)),
    )
