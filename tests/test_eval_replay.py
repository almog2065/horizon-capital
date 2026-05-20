"""Eval-harness replay tests — deterministic, no network."""
from __future__ import annotations

from evals.replay import _builtin_sample, replay


def test_builtin_sample_shape():
    s = _builtin_sample()
    assert s["starting_nav"] > 0
    assert isinstance(s["events"], list) and len(s["events"]) >= 1
    for ev in s["events"]:
        assert "ticker" in ev
        assert ev["action"] in ("buy", "sell")
        assert "price" in ev and ev["price"] > 0


def test_replay_returns_result():
    r = replay("sample")
    assert r.starting_nav > 0
    assert len(r.equity_curve) >= 2  # start + at least one step
    assert r.trades, "expected trades"
    assert r.traces, "expected traces"


def test_replay_traces_include_grounded_llm_call():
    r = replay("sample")
    llm_traces = [t for t in r.traces if t.get("kind") == "llm_call"]
    assert llm_traces, "expected llm_call traces"
    # at least one should have citations
    assert any(t.get("citations") for t in llm_traces)


def test_replay_records_hitl_when_flagged():
    r = replay("sample")
    hitl_required = [t for t in r.traces if t.get("event") == "hitl_required"]
    hitl_resolved = [t for t in r.traces if t.get("event") == "hitl_resolved"]
    assert len(hitl_required) == len(hitl_resolved)
