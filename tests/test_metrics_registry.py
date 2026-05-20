from app import db, traces
from app.metrics_registry import build_ops_summary, build_prometheus_text, observe_llm_call


def _boot_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "FIRM_DB", tmp_path / "firm.sqlite")
    monkeypatch.setattr(traces.config, "FIRM_DB", tmp_path / "firm.sqlite")
    db.init_db()
    traces.init_db()


def test_prometheus_text_includes_core_metrics(tmp_path, monkeypatch):
    _boot_db(tmp_path, monkeypatch)
    observe_llm_call(
        purpose="fundamental",
        model="gpt-4o",
        mode="live",
        prompt_tokens=1000,
        completion_tokens=500,
        cost_usd=0.01,
        duration_ms=1200,
    )
    text = build_prometheus_text()
    assert "horizon_llm_calls_total" in text
    assert "horizon_hitl_pending" in text
    assert "horizon_trace_llm_cost_usd" in text


def test_ops_summary_shape(tmp_path, monkeypatch):
    _boot_db(tmp_path, monkeypatch)
    summary = build_ops_summary()
    assert "llm_usage_today" in summary
    assert "et_now" in summary
