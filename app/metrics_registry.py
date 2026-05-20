"""Prometheus metrics and ops summaries — no extra dependencies.

Counters increment on hot paths (LLM calls, cadence jobs). Gauges are
refreshed on each /metrics scrape from SQLite (runs, HITL, traces).
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from .core.logging import get_logger

log = get_logger("horizon.metrics")

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
    with _lock:
        _counters[key] += value


def observe_llm_call(
    *,
    purpose: str,
    model: str,
    mode: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    duration_ms: int,
) -> None:
    inc("horizon_llm_calls_total", purpose=purpose, model=model, mode=mode)
    inc("horizon_llm_prompt_tokens_total", value=max(0, prompt_tokens), purpose=purpose, model=model)
    inc("horizon_llm_completion_tokens_total", value=max(0, completion_tokens), purpose=purpose, model=model)
    inc("horizon_llm_cost_usd_total", value=cost_usd, purpose=purpose, model=model)
    inc("horizon_llm_duration_ms_total", value=max(0, duration_ms), purpose=purpose)


def observe_cadence_job(job_id: str, *, ok: bool = True) -> None:
    inc("horizon_cadence_jobs_total", job_id=job_id, status="ok" if ok else "error")


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _emit_counter(lines: list[str], name: str, help_text: str, samples: dict[tuple, float]) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    for key, val in sorted(samples.items()):
        if not key[0] == name:
            continue
        lbl = dict(key[1]) if len(key) > 1 else {}
        lines.append(f"{name}{_fmt_labels(lbl)} {val}")


def _emit_gauge(lines: list[str], name: str, help_text: str, value: float, **labels: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    lines.append(f"{name}{_fmt_labels(labels)} {value}")


def _load_last_eval() -> dict[str, Any]:
    """Most recent eval artifact for horizon_eval_* gauges."""
    root = Path(__file__).resolve().parents[1] / "evals" / "output"
    if not root.exists():
        return {}
    candidates = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidates[:5]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "portfolio" in data and "process" in data:
                data["_path"] = str(p)
                return data
        except Exception:
            continue
    return {}


def build_prometheus_text() -> str:
    from . import config, db, daily_plan
    from .market_calendar import is_equity_session_open, is_trading_day
    from . import traces

    lines: list[str] = []

    with _lock:
        counter_snapshot = dict(_counters)

    # In-memory counters
    by_name: dict[str, dict[tuple, float]] = defaultdict(dict)
    for (name, lbls), val in counter_snapshot.items():
        by_name[name][(name, lbls)] = val

    for name, samples in by_name.items():
        help_map = {
            "horizon_llm_calls_total": "LLM invocations since process start.",
            "horizon_llm_prompt_tokens_total": "Prompt tokens consumed.",
            "horizon_llm_completion_tokens_total": "Completion tokens consumed.",
            "horizon_llm_cost_usd_total": "Estimated LLM spend (USD).",
            "horizon_llm_duration_ms_total": "LLM latency sum (ms).",
            "horizon_cadence_jobs_total": "Cadence job executions.",
        }
        _emit_counter(lines, name, help_map.get(name, name), samples)

    # DB + trace aggregates (gauges)
    try:
        runs = db.list_runs(2000)
        hitl_pending = len(db.list_hitl_pending())
        by_status: dict[str, int] = defaultdict(int)
        by_trigger: dict[str, int] = defaultdict(int)
        for r in runs:
            by_status[r.get("status") or "unknown"] += 1
            by_trigger[r.get("trigger_type") or "unknown"] += 1

        _emit_gauge(lines, "horizon_runs_total", "Firm runs in DB (recent sample).", len(runs))
        _emit_gauge(lines, "horizon_hitl_pending", "Pending HITL queue depth.", hitl_pending)
        for st, n in by_status.items():
            _emit_gauge(lines, "horizon_runs_by_status", "Runs by status.", n, status=st)
        for tt, n in list(by_trigger.items())[:12]:
            _emit_gauge(lines, "horizon_runs_by_trigger", "Runs by trigger_type.", n, trigger_type=tt)

        usage = traces.aggregate_usage()
        _emit_gauge(lines, "horizon_trace_llm_calls", "LLM calls in trace DB (all time).", usage["llm_calls"])
        _emit_gauge(lines, "horizon_trace_llm_tokens_total", "Total tokens in trace DB.", usage["tokens_total"])
        _emit_gauge(lines, "horizon_trace_llm_cost_usd", "Estimated LLM cost from traces (USD).", usage["cost_usd"])
        _emit_gauge(lines, "horizon_trace_rag_calls", "RAG retrievals in trace DB.", usage["rag_calls"])
        _emit_gauge(lines, "horizon_trace_tool_calls", "Tool calls in trace DB.", usage["tool_calls"])

        day_usage = traces.aggregate_usage(since_ts=traces.trading_day_start_ts())
        _emit_gauge(lines, "horizon_llm_cost_usd_today", "Estimated LLM cost today (ET, USD).", day_usage["cost_usd"])
        _emit_gauge(lines, "horizon_llm_calls_today", "LLM calls today (ET).", day_usage["llm_calls"])
    except Exception as e:
        log.warning("metrics-db-snapshot-failed: %s", e)

    try:
        plan = daily_plan.load()
        done = len(plan.get("completed_jobs") or [])
        _emit_gauge(lines, "horizon_cadence_jobs_done_today", "Cadence jobs completed today.", done)
        _emit_gauge(lines, "horizon_market_session_open", "1 if US equity session open.", 1 if is_equity_session_open() else 0)
        _emit_gauge(lines, "horizon_trading_day", "1 if weekday ET.", 1 if is_trading_day() else 0)
    except Exception as e:
        log.warning("metrics-cadence-snapshot-failed: %s", e)

    try:
        from . import ops_alerts
        summ = ops_alerts.summary()
        _emit_gauge(lines, "horizon_alerts_open", "Unacknowledged ops alerts.", summ["open"])
        for sev, n in (summ.get("by_severity") or {}).items():
            _emit_gauge(lines, "horizon_alerts_open_by_severity", "Open alerts by severity.", n, severity=sev)
    except Exception:
        pass

    ev = _load_last_eval()
    if ev:
        port = ev.get("portfolio") or {}
        proc = ev.get("process") or {}
        cost = ev.get("cost") or {}
        _emit_gauge(lines, "horizon_eval_pnl_pct", "Last eval window P&L %.", float(port.get("pnl_pct") or 0))
        _emit_gauge(lines, "horizon_eval_grounded_ratio", "Last eval grounded ratio.", float(proc.get("grounded_ratio") or 0))
        _emit_gauge(lines, "horizon_eval_guardrail_breaches", "Last eval guardrail breaches.", float(proc.get("guardrail_breaches") or 0))
        _emit_gauge(lines, "horizon_eval_estimated_cost_usd", "Last eval estimated LLM cost.", float(cost.get("total_usd") or 0))

    return "\n".join(lines) + "\n"


def build_ops_summary() -> dict[str, Any]:
    """JSON snapshot for diagnostics UI and /api/observability/summary."""
    from . import db, traces, daily_plan
    from . import daily_cadence
    from .market_calendar import is_equity_session_open, to_et

    empty_usage = {
        "llm_calls": 0, "rag_calls": 0, "tool_calls": 0,
        "tokens_total": 0, "cost_usd": 0.0,
    }
    try:
        usage_all = traces.aggregate_usage()
        usage_day = traces.aggregate_usage(since_ts=traces.trading_day_start_ts())
        by_purpose = traces.aggregate_usage_by_purpose(since_ts=traces.trading_day_start_ts())
    except Exception:
        usage_all = usage_day = empty_usage
        by_purpose = []

    try:
        runs_recent = db.list_runs(30)
    except Exception:
        runs_recent = []
    eval_last = _load_last_eval()

    return {
        "ts": time.time(),
        "et_now": to_et().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "session_open": is_equity_session_open(),
        "llm_usage_all_time": usage_all,
        "llm_usage_today": usage_day,
        "llm_by_purpose_today": by_purpose[:15],
        "cadence": {
            "completed_today": daily_plan.load().get("completed_jobs") or [],
            "metrics": daily_cadence.cadence_metrics(),
        },
        "runs_recent": [
            {
                "run_id": r.get("run_id"),
                "trigger_type": r.get("trigger_type"),
                "status": r.get("status"),
            }
            for r in runs_recent[:12]
        ],
        "hitl_pending": len(db.list_hitl_pending()),
        "last_eval": {
            "window": eval_last.get("window"),
            "portfolio": eval_last.get("portfolio"),
            "process": eval_last.get("process"),
            "cost": eval_last.get("cost"),
            "path": eval_last.get("_path"),
        } if eval_last else None,
    }
