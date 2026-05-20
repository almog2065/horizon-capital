"""Eval dashboard UI helpers."""
from __future__ import annotations

from app import eval_dashboard


def test_list_scenarios_includes_sample():
    scenarios = eval_dashboard.list_scenarios()
    ids = {s["id"] for s in scenarios}
    assert "sample" in ids


def test_normalize_legacy_report():
    legacy = {
        "window": "old",
        "portfolio": {
            "pnl_absolute": 100.0,
            "pnl_pct": 0.01,
            "benchmark_pct": 0.45,
            "excess_return_pct": -0.44,
            "max_drawdown_pct": 0.0,
            "hit_rate": 100.0,
            "n_trades": 1,
        },
        "process": {"grounded_ratio": 1.0, "guardrail_breaches": 0},
    }
    out = eval_dashboard.normalize_report(legacy)
    assert out["benchmark"]["symbol"] == "SPY"
    assert out["benchmark"]["return_pct"] == 0.45
    assert "decision_quality" in out["process"]


def test_is_stale_report():
    assert eval_dashboard.is_stale_report({"portfolio": {}, "process": {}})
    assert not eval_dashboard.is_stale_report({
        "benchmark": {"symbol": "SPY"},
        "process": {
            "decision_quality": 1.0,
            "guardrail_effectiveness": 1.0,
            "guardrail_checks": 1,
            "hitl_discipline": 1.0,
        },
    })


def test_normalize_trades_without_realized_pnl():
    out = eval_dashboard.normalize_report({
        "benchmark": {"symbol": "SPY", "return_pct": 0.0, "source": "x"},
        "process": {
            "decision_quality": 1.0,
            "guardrail_effectiveness": 1.0,
            "guardrail_checks": 0,
            "hitl_discipline": 1.0,
        },
        "trades": [
            {"ticker": "MSFT", "side": "buy", "qty": 100, "price": 400.0},
            {"ticker": "MSFT", "side": "sell", "qty": 50, "price": 410.0, "realized_pnl": 500.0},
        ],
    })
    assert out["trades"][0]["realized_pnl"] is None
    assert out["trades"][1]["realized_pnl"] == 500.0


def test_run_and_save_report(tmp_path, monkeypatch):
    out = tmp_path / "evals" / "output"
    data = tmp_path / "evals" / "data"
    data.mkdir(parents=True)
    out.mkdir(parents=True)
    (data / "sample.json").write_text(
        '{"window":"sample","starting_nav":1000000,"benchmark_symbol":"SPY",'
        '"benchmark_pct":0.45,"events":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_dashboard, "EVALS_DATA", data)
    monkeypatch.setattr(eval_dashboard, "EVALS_OUTPUT", out)

    report = eval_dashboard.run_and_save("sample")
    assert report["benchmark"]["symbol"] == "SPY"
    assert "portfolio" in report
    assert (out / "sample.json").is_file()
