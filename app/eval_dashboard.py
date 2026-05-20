"""Eval harness UI — load scenarios, run replay, expose report for templates."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .core.logging import get_logger

log = get_logger("horizon.eval_dashboard")

ROOT = Path(__file__).resolve().parents[1]
EVALS_DATA = ROOT / "evals" / "data"
EVALS_OUTPUT = ROOT / "evals" / "output"


def list_scenarios() -> list[dict[str, str]]:
    """Scenario windows from evals/data/*.json (excludes spy_benchmarks)."""
    out: list[dict[str, str]] = []
    if not EVALS_DATA.exists():
        return out
    for p in sorted(EVALS_DATA.glob("*.json")):
        if p.name.startswith("spy_"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        window = str(data.get("window") or p.stem)
        out.append({
            "id": p.stem,
            "window": window,
            "label": window,
            "events": len(data.get("events") or []),
        })
    return out


def report_path_for(window: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in window)
    return EVALS_OUTPUT / f"{safe}.json"


def load_report(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return normalize_report(raw)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("eval-report-read-failed path=%s err=%s", path, e)
        return None


def is_stale_report(raw: dict) -> bool:
    """True when cached JSON predates current report schema."""
    if not raw.get("benchmark"):
        return True
    proc = raw.get("process") or {}
    required_proc = (
        "decision_quality",
        "guardrail_effectiveness",
        "guardrail_checks",
        "hitl_discipline",
    )
    return any(k not in proc for k in required_proc)


def normalize_report(raw: dict[str, Any]) -> dict[str, Any]:
    """Fill missing sections so templates work with older evals/output/*.json."""
    p = dict(raw.get("portfolio") or {})
    raw["portfolio"] = p
    proc = dict(raw.get("process") or {})
    raw["process"] = proc
    raw["cost"] = dict(raw.get("cost") or {})
    trades: list[dict[str, Any]] = []
    for t in raw.get("trades") or []:
        if isinstance(t, dict):
            row = dict(t)
            row.setdefault("realized_pnl", None)
            trades.append(row)
    raw["trades"] = trades
    raw.setdefault("trace_summary", raw.get("trace_summary") or {"n_events": 0})

    bench = raw.get("benchmark")
    if not isinstance(bench, dict):
        raw["benchmark"] = {
            "symbol": "SPY",
            "return_pct": float(p.get("benchmark_pct") or 0.0),
            "source": "legacy",
        }
    else:
        bench.setdefault("symbol", "SPY")
        bench.setdefault("return_pct", float(p.get("benchmark_pct") or 0.0))
        bench.setdefault("source", bench.get("source") or "scenario")

    proc.setdefault("grounded_ratio", proc.get("grounded_ratio", 0.0))
    proc.setdefault("decision_quality", proc.get("decision_quality", 0.0))
    proc.setdefault("guardrail_effectiveness", proc.get("guardrail_effectiveness", 1.0))
    proc.setdefault("guardrail_checks", proc.get("guardrail_checks", 0))
    proc.setdefault("guardrail_passed", proc.get("guardrail_passed", 0))
    proc.setdefault("guardrail_breaches", proc.get("guardrail_breaches", 0))
    proc.setdefault("hitl_discipline", proc.get("hitl_discipline", 1.0))
    proc.setdefault("hitl_required", proc.get("hitl_required", 0))
    proc.setdefault("hitl_resolved", proc.get("hitl_resolved", 0))
    proc.setdefault("grounded_calls", proc.get("grounded_calls", 0))
    proc.setdefault("n_llm_calls", proc.get("n_llm_calls", 0))
    proc.setdefault("citations_per_decision", proc.get("citations_per_decision", 0.0))
    proc.setdefault("refusal_count", proc.get("refusal_count", 0))
    proc.setdefault("n_agent_calls", proc.get("n_agent_calls", 0))

    raw["cost"].setdefault("total_usd", 0.0)
    raw["cost"].setdefault("n_llm_calls", 0)
    raw["cost"].setdefault("prompt_tokens", 0)
    raw["cost"].setdefault("completion_tokens", 0)
    return raw


def run_and_save(window: str = "sample") -> dict[str, Any]:
    """Execute eval harness for ``window`` and write JSON report."""
    from evals.metrics import compute_cost, compute_portfolio, compute_process
    from evals.replay import replay

    EVALS_OUTPUT.mkdir(parents=True, exist_ok=True)
    result = replay(window)
    pm = compute_portfolio(
        starting_nav=result.starting_nav,
        equity_curve=result.equity_curve,
        benchmark_pct=result.benchmark_pct,
        trades=result.trades,
    )
    proc = compute_process(result.traces)
    cost = compute_cost(result.traces)

    report: dict[str, Any] = {
        "window": result.window,
        "reproducible": True,
        "mock_llm": True,
        "benchmark": {
            "symbol": result.benchmark_symbol,
            "return_pct": result.benchmark_pct,
            "source": result.benchmark_source,
        },
        "portfolio": pm.as_dict(),
        "process": proc.as_dict(),
        "cost": cost.as_dict(),
        "equity_curve": result.equity_curve,
        "trades": result.trades,
        "trace_summary": {
            "n_events": len(result.traces),
            "kinds": sorted({t.get("kind") for t in result.traces if t.get("kind")}),
        },
    }
    out = report_path_for(window)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    log.info("eval-report-saved window=%s path=%s", window, out)
    return report


def get_report(
    window: str = "sample",
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return cached report or run harness if missing / refresh requested."""
    path = report_path_for(window)
    if not refresh:
        cached = load_report(path)
        if cached and not is_stale_report(cached):
            cached.setdefault("_meta", {})["source_path"] = str(path)
            cached["_meta"]["cached"] = True
            return cached
        if cached and is_stale_report(cached):
            log.info("eval-report-stale-refresh window=%s path=%s", window, path)
    report = run_and_save(window)
    report = normalize_report(report)
    report.setdefault("_meta", {})["source_path"] = str(path)
    report["_meta"]["cached"] = False
    return report


def build_page_context(
    window: str = "sample",
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    scenarios = list_scenarios()
    scenario_ids = {s["id"] for s in scenarios}
    if window not in scenario_ids and scenarios:
        window = scenarios[0]["id"]

    report: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    try:
        report = get_report(window, refresh=refresh)
    except Exception as e:
        log.exception("eval-page-failed window=%s", window)
        error = str(e)

    return {
        "window": window,
        "scenarios": scenarios,
        "report": report,
        "error": error,
        "has_report": report is not None,
    }
