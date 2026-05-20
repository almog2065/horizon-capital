"""Eval harness end-to-end — reproducibility, SPY benchmark, guardrails, CI entry."""
from __future__ import annotations

import json
import subprocess
import sys

from evals.benchmark import resolve_benchmark
from evals.metrics import compute_portfolio, compute_process
from evals.replay import load_scenario, replay


def test_replay_is_deterministic():
    a = replay("sample")
    b = replay("sample")
    assert a.equity_curve == b.equity_curve
    assert a.trades == b.trades
    assert len(a.traces) == len(b.traces)


def test_spy_benchmark_metadata():
    r = replay("sample")
    assert r.benchmark_symbol == "SPY"
    assert r.benchmark_pct == 0.45
    scenario = load_scenario("sample")
    meta = resolve_benchmark(scenario)
    assert meta["symbol"] == "SPY"
    assert meta["return_pct"] == 0.45


def test_portfolio_excess_vs_spy():
    r = replay("sample")
    pm = compute_portfolio(r.starting_nav, r.equity_curve, r.benchmark_pct, r.trades)
    assert pm.benchmark_pct == r.benchmark_pct
    assert pm.excess_return_pct == pm.pnl_pct - pm.benchmark_pct


def test_guardrail_block_fixture():
    r = replay("guardrail_block")
    proc = compute_process(r.traces)
    assert proc.guardrail_breaches >= 1
    assert proc.guardrail_effectiveness < 1.0
    # oversized order must not appear in fills
    assert not any(t["ticker"] == "META" for t in r.trades)


def test_eval_run_cli_exits_zero():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.run",
            "--window",
            "sample",
            "--out",
            "evals/output/pytest-ci.json",
            "--fail-on",
            "grounded_ratio:0.75",
            "--fail-on",
            "decision_quality:0.70",
            "--fail-on",
            "guardrail_breaches:0",
        ],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "USE_MOCK_LLM": "1"},
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(
        (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "evals/output/pytest-ci.json").read_text(encoding="utf-8")
    )
    assert report["benchmark"]["symbol"] == "SPY"
    assert "decision_quality" in report["process"]
    assert report["reproducible"] is True
