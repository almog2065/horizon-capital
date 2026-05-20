"""Activity pipeline view for run traces."""
from __future__ import annotations

import time

from app import config, db, traces


def test_build_trace_pipeline_groups_agent_children(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    db.init_db()
    traces.init_db()

    run_id = "run_tp_test"
    traces.set_context(run_id=run_id, agent="fundamental")

    time.time()
    traces.record("agent_start", {"agent": "fundamental"}, duration_ms=0)
    traces.record("llm_call", {"purpose": "thesis", "model": "gpt-4o"}, duration_ms=100)
    traces.record("tool_call", {"tool": "get_quote", "status": "ok"}, duration_ms=50)
    traces.record("agent_end", {"agent": "fundamental"}, duration_ms=200)

    pipe = traces.build_trace_pipeline(run_id)
    assert pipe["total_steps"] >= 1
    agent_steps = [s for s in pipe["steps"] if s["kind"] == "agent"]
    assert agent_steps, "expected agent milestone"
    assert len(agent_steps[0]["children"]) == 2
    kinds = {c["kind"] for c in agent_steps[0]["children"]}
    assert kinds == {"llm", "tool"}

    inv = agent_steps[0]["call_inventory"]
    assert len(inv["llm"]) == 1
    assert inv["llm"][0]["label"] == "thesis"
    assert inv["llm"][0]["detail"] == "gpt-4o"
    assert len(inv["tool"]) == 1
    assert inv["tool"][0]["label"] == "get_quote"
    assert agent_steps[0]["subtitle"] == "1 LLM · 1 tool"


def test_call_inventory_multiple_tools_and_rag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    db.init_db()
    traces.init_db()

    run_id = "run_tp_multi"
    traces.set_context(run_id=run_id, agent="research")

    traces.record("agent_start", {"agent": "research"}, duration_ms=0)
    traces.record("llm_call", {"purpose": "summarize", "model": "gpt-4o-mini"}, duration_ms=80)
    traces.record("tool_call", {"tool": "get_quote", "status": "ok"}, duration_ms=40)
    traces.record("tool_call", {"tool": "fetch_fundamentals", "status": "ok"}, duration_ms=60)
    traces.record("rag_retrieval", {"corpus": "filings", "hits_count": 3}, duration_ms=30)
    traces.record("agent_end", {"agent": "research"}, duration_ms=250)

    inv = traces.build_trace_pipeline(run_id)["steps"][0]["call_inventory"]
    assert len(inv["llm"]) == 1
    assert len(inv["tool"]) == 2
    assert [t["label"] for t in inv["tool"]] == ["get_quote", "fetch_fundamentals"]
    assert len(inv["rag"]) == 1
    assert inv["rag"][0]["label"] == "filings"
    assert inv["rag"][0]["detail"] == "3 hits"


def test_build_call_inventory_from_traces_for_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    db.init_db()
    traces.init_db()

    run_id = "run_journal_inv"
    traces.set_context(run_id=run_id, agent="fundamental")
    traces.record("llm_call", {"purpose": "thesis", "model": "gpt-4o"}, duration_ms=100)
    traces.record("tool_call", {"tool": "get_quote", "status": "ok"}, duration_ms=50)

    rows = traces.list_for_run(run_id)
    inv = traces.build_call_inventory_from_traces(rows)
    assert inv["llm"][0]["label"] == "thesis"
    assert inv["tool"][0]["label"] == "get_quote"
