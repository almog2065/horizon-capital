"""Eval-harness pure-function tests (no IO, no LLM, deterministic)."""
from __future__ import annotations

import math

from evals.metrics import (
    PortfolioMetrics,
    ProcessMetrics,
    compute_decision_quality,
    compute_portfolio,
    compute_process,
    hit_rate,
    max_drawdown,
)


# ---------- max_drawdown ----------
def test_max_drawdown_empty():
    assert max_drawdown([]) == 0.0


def test_max_drawdown_monotonic():
    assert max_drawdown([100, 110, 120]) == 0.0


def test_max_drawdown_simple():
    # peak=120, trough=90 -> (120-90)/120 = 0.25
    assert math.isclose(max_drawdown([100, 120, 90, 95]), 0.25, abs_tol=1e-9)


def test_max_drawdown_two_phases():
    # peak1=200 trough=150 (-25%), then peak2=180 trough=120 (-33%)
    # global max DD = max from any peak
    dd = max_drawdown([100, 200, 150, 180, 120])
    # from peak 200, trough 120 = 0.40 — that's the biggest
    assert math.isclose(dd, 0.4, abs_tol=1e-9)


# ---------- hit_rate ----------
def test_hit_rate_empty():
    assert hit_rate([]) == 0.0


def test_hit_rate_no_closed():
    # open trades (no realized_pnl) shouldn't count
    assert hit_rate([{"side": "buy", "price": 10}]) == 0.0


def test_hit_rate_basic():
    trades = [
        {"realized_pnl": 100},
        {"realized_pnl": -50},
        {"realized_pnl": 0},  # 0 counts as non-negative
    ]
    assert math.isclose(hit_rate(trades), 2 / 3, abs_tol=1e-9)


# ---------- compute_portfolio ----------
def test_compute_portfolio_no_movement():
    pm = compute_portfolio(1000.0, [1000.0], 0.0, [])
    assert isinstance(pm, PortfolioMetrics)
    assert pm.pnl_absolute == 0.0
    assert pm.pnl_pct == 0.0
    assert pm.max_drawdown_pct == 0.0
    assert pm.n_trades == 0


def test_compute_portfolio_basic():
    pm = compute_portfolio(
        starting_nav=1000.0,
        equity_curve=[1000.0, 1100.0, 1050.0, 1200.0],
        benchmark_pct=5.0,
        trades=[{"realized_pnl": 50}, {"realized_pnl": -10}],
    )
    assert pm.pnl_absolute == 200.0
    assert pm.pnl_pct == 20.0
    assert pm.excess_return_pct == 15.0
    # peak 1200 vs trough between (1100->1050 dip): worst DD =
    # peak before final reached at 1100, trough 1050 = 0.0454545
    assert pm.max_drawdown_pct > 0
    assert pm.n_trades == 2
    assert math.isclose(pm.hit_rate, 50.0, abs_tol=1e-9)


# ---------- compute_process ----------
def test_compute_process_empty():
    proc = compute_process([])
    assert isinstance(proc, ProcessMetrics)
    assert proc.n_llm_calls == 0
    assert proc.grounded_ratio == 0.0
    assert proc.guardrail_breaches == 0
    assert proc.guardrail_effectiveness == 1.0
    assert proc.decision_quality == 0.0


def test_compute_process_counts():
    traces = [
        {"kind": "agent_call"},
        {"kind": "agent_call"},
        {"kind": "llm_call", "citations": ["a"]},
        {"kind": "llm_call", "citations": ["a", "b"]},
        {"kind": "llm_call", "citations": []},
        {"event": "hitl_required"},
        {"event": "hitl_resolved"},
        {"event": "guardrail_check", "passed": True},
        {"event": "guardrail_check", "passed": False},
        {"event": "guardrail_breach"},
        {"kind": "agent_call", "outcome": "refused"},
    ]
    proc = compute_process(traces)
    assert proc.n_agent_calls == 3
    assert proc.n_llm_calls == 3
    assert proc.grounded_calls == 2
    assert proc.grounded_ratio == 2 / 3
    assert proc.citations_per_decision == 1.0   # 3 citations / 3 llm calls
    assert proc.refusal_count == 1
    assert proc.hitl_required == 1
    assert proc.hitl_resolved == 1
    assert proc.guardrail_checks == 2
    assert proc.guardrail_passed == 1
    assert proc.guardrail_breaches == 1
    assert proc.guardrail_effectiveness == 0.5
    assert proc.hitl_discipline == 1.0
    assert 0.0 < proc.decision_quality < 1.0


def test_compute_decision_quality_bounds():
    q = compute_decision_quality(
        grounded_ratio=1.0,
        hitl_discipline=1.0,
        guardrail_effectiveness=1.0,
        approval_rate=1.0,
    )
    assert q == 1.0
