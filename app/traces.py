"""Trace events - record every LLM call, tool call, RAG retrieval, agent step.

Two layers:
1. Local trace events stored in SQLite, shown in the UI (waterfall per run).
2. LangSmith nested runs (chain → tool/llm/retriever) when API key is set.
"""
from __future__ import annotations
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, time as dt_time, timezone
import contextvars
import os
import uuid
from contextlib import contextmanager, nullcontext
from typing import Optional, Any, Iterator
from . import config

_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_run_id", default=None)
_current_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_agent", default=None)
_current_journal_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_journal_id", default=None)
_current_local_parent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_local_parent", default=None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    run_id TEXT,
    agent TEXT,
    journal_id INTEGER,
    event_type TEXT,
    event_data TEXT,
    duration_ms INTEGER,
    ts REAL,
    parent_trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id);
CREATE INDEX IF NOT EXISTS idx_traces_agent ON traces(run_id, agent);
"""


def init_db():
    config.FIRM_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.FIRM_DB) as c:
        c.executescript(SCHEMA)


def configure_langsmith() -> dict:
    """Sync LangSmith / LangChain tracing env vars at startup."""
    key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    if not key:
        return {"configured": False, "reason": "no_api_key"}

    project = (
        os.environ.get("LANGSMITH_PROJECT")
        or os.environ.get("LANGCHAIN_PROJECT")
        or "horizon-capital"
    )
    tracing_on = os.environ.get("LANGSMITH_TRACING", "true").lower() in (
        "1", "true", "yes", "on",
    )
    if tracing_on:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_TRACING"] = "true"

    os.environ.setdefault("LANGCHAIN_API_KEY", key)
    os.environ.setdefault("LANGSMITH_API_KEY", key)
    os.environ.setdefault("LANGCHAIN_PROJECT", project)
    os.environ.setdefault("LANGSMITH_PROJECT", project)

    return {
        "configured": True,
        "project": project,
        "tracing_v2": os.environ.get("LANGCHAIN_TRACING_V2"),
    }


def set_context(run_id: Optional[str] = None, agent: Optional[str] = None,
                journal_id: Optional[int] = None):
    if run_id is not None:
        _current_run_id.set(run_id)
    if agent is not None:
        _current_agent.set(agent)
    if journal_id is not None:
        _current_journal_id.set(journal_id)


@contextmanager
def agent_context(run_id: str, agent: str):
    rt = _current_run_id.set(run_id)
    at = _current_agent.set(agent)
    try:
        yield
    finally:
        _current_run_id.reset(rt)
        _current_agent.reset(at)


@contextmanager
def journal_context(journal_id: int):
    tok = _current_journal_id.set(journal_id)
    try:
        yield
    finally:
        _current_journal_id.reset(tok)


def record(event_type: str, event_data: dict, duration_ms: int = 0,
           parent_trace_id: Optional[str] = None) -> str:
    trace_id = "tr_" + uuid.uuid4().hex[:12]
    run_id = _current_run_id.get()
    agent = _current_agent.get()
    journal_id = _current_journal_id.get()
    parent = parent_trace_id or _current_local_parent.get()

    try:
        with sqlite3.connect(config.FIRM_DB) as c:
            c.execute(
                "INSERT INTO traces (trace_id, run_id, agent, journal_id, "
                "event_type, event_data, duration_ms, ts, parent_trace_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (trace_id, run_id, agent, journal_id, event_type,
                 json.dumps(event_data, default=str), duration_ms, time.time(),
                 parent),
            )
    except Exception as e:
        print(f"[trace] failed to persist: {e}")

    return trace_id


def _json_preview(obj: Any, limit: int = 4000) -> str:
    return json.dumps(obj, default=str)[:limit]


@contextmanager
def ls_span(
    name: str,
    run_type: str = "chain",
    *,
    inputs: Optional[dict] = None,
    metadata: Optional[dict] = None,
    tags: Optional[list[str]] = None,
) -> Iterator[Any]:
    """Nested LangSmith span; also sets local parent for child trace events."""
    if not langsmith_enabled() or os.environ.get("LANGCHAIN_TRACING_V2", "").lower() not in (
        "1", "true", "yes", "on",
    ):
        yield None
        return

    try:
        from langsmith.run_helpers import trace
    except ImportError:
        yield None
        return

    run_id = _current_run_id.get()
    agent = _current_agent.get()
    meta = dict(metadata or {})
    if run_id:
        meta["horizon_run_id"] = run_id
    if agent:
        meta["horizon_agent"] = agent
    tags = list(tags or [])
    if agent and agent not in tags:
        tags.append(agent)

    local_start = record(
        "span_start",
        {
            "name": name,
            "run_type": run_type,
            "inputs_preview": _json_preview(inputs or {}, 2000),
        },
    )
    prev_parent = _current_local_parent.set(local_start)

    try:
        with trace(
            name,
            run_type=run_type,
            inputs=inputs or {},
            metadata=meta,
            tags=tags,
        ) as ls_run:
            yield ls_run
            if ls_run is not None:
                record(
                    "span_end",
                    {
                        "name": name,
                        "langsmith_run_id": str(getattr(ls_run, "id", "")),
                    },
                    parent_trace_id=local_start,
                )
    finally:
        _current_local_parent.reset(prev_parent)


def record_rag_search(
    corpus: str,
    query: str,
    hits: list[dict],
    top_k: int,
    duration_ms: int,
    metadata_filter: Optional[dict] = None,
    embed_mode: str = "unknown",
) -> str:
    """Local + LangSmith retriever span for RAG."""
    hits_summary = [
        {
            "chunk_id": h.get("chunk_id"),
            "score": round(float(h.get("score", 0)), 4),
            "text_preview": (h.get("text") or "")[:280],
            "metadata": h.get("metadata") or {},
        }
        for h in hits[:top_k]
    ]
    event = {
        "corpus": corpus,
        "query": query,
        "top_k": top_k,
        "hits_count": len(hits),
        "metadata_filter": metadata_filter,
        "embed_mode": embed_mode,
        "hits": hits_summary,
    }
    parent = record("rag_retrieval", event, duration_ms=duration_ms)

    if langsmith_enabled():
        try:
            with ls_span(
                f"rag.search.{corpus}",
                run_type="retriever",
                inputs={
                    "corpus": corpus,
                    "query": query,
                    "top_k": top_k,
                    "metadata_filter": metadata_filter,
                },
                metadata={"corpus": corpus, "embed_mode": embed_mode},
                tags=["rag", corpus],
            ) as ls_run:
                if ls_run is not None:
                    ls_run.end(outputs={
                        "hits_count": len(hits),
                        "hits": hits_summary,
                    })
        except Exception as e:
            print(f"[langsmith] rag span failed: {e}")

    return parent


def trading_day_start_ts() -> float:
    """Unix timestamp for midnight ET today."""
    from .market_calendar import ET, trading_date
    from datetime import datetime, time as dt_time

    day = trading_date()
    start_et = datetime.combine(day, dt_time.min, tzinfo=ET)
    return start_et.timestamp()


def aggregate_usage(*, since_ts: float = 0) -> dict[str, Any]:
    """Sum LLM/RAG/tool activity from the traces table."""
    from . import model_routing

    llm_calls = 0
    rag_calls = 0
    tool_calls = 0
    prompt_tok = 0
    completion_tok = 0
    cost_usd = 0.0

    with sqlite3.connect(config.FIRM_DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT event_type, event_data, duration_ms FROM traces WHERE ts >= ?",
            (since_ts,),
        ).fetchall()

    for r in rows:
        et = r["event_type"]
        data = json.loads(r["event_data"] or "{}")
        if et == "llm_call":
            llm_calls += 1
            tok = data.get("tokens") or {}
            p = int(tok.get("prompt") or 0)
            c_tok = int(tok.get("completion") or 0)
            prompt_tok += p
            completion_tok += c_tok
            cost_usd += float(data.get("estimated_cost_usd") or 0)
            if not data.get("estimated_cost_usd") and p + c_tok:
                cost_usd += model_routing.estimate_cost_usd(
                    str(data.get("model") or ""),
                    p,
                    c_tok,
                )
        elif et == "rag_retrieval":
            rag_calls += 1
        elif et == "tool_call":
            tool_calls += 1

    return {
        "llm_calls": llm_calls,
        "rag_calls": rag_calls,
        "tool_calls": tool_calls,
        "prompt_tokens": prompt_tok,
        "completion_tokens": completion_tok,
        "tokens_total": prompt_tok + completion_tok,
        "cost_usd": round(cost_usd, 4),
    }


def aggregate_usage_by_purpose(*, since_ts: float = 0, limit: int = 20) -> list[dict]:
    from . import model_routing

    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "cost_usd": 0.0, "duration_ms": 0},
    )
    with sqlite3.connect(config.FIRM_DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT event_data, duration_ms FROM traces "
            "WHERE event_type='llm_call' AND ts >= ?",
            (since_ts,),
        ).fetchall()
    for r in rows:
        data = json.loads(r["event_data"] or "{}")
        purpose = str(data.get("purpose") or "unknown")
        b = buckets[purpose]
        b["calls"] += 1
        tok = data.get("tokens") or {}
        p = int(tok.get("prompt") or 0)
        c_tok = int(tok.get("completion") or 0)
        b["tokens"] += p + c_tok
        cst = float(data.get("estimated_cost_usd") or 0)
        if not cst and p + c_tok:
            cst = model_routing.estimate_cost_usd(str(data.get("model") or ""), p, c_tok)
        b["cost_usd"] += cst
        b["duration_ms"] += int(r["duration_ms"] or 0)
        b["model"] = data.get("model")

    out = [
        {
            "purpose": k,
            "calls": v["calls"],
            "tokens": v["tokens"],
            "cost_usd": round(v["cost_usd"], 4),
            "avg_duration_ms": int(v["duration_ms"] / v["calls"]) if v["calls"] else 0,
            "model": v.get("model"),
        }
        for k, v in buckets.items()
    ]
    out.sort(key=lambda x: -x["cost_usd"])
    return out[:limit]


def record_llm_call(
    *,
    purpose: str,
    model: str,
    mode: str,
    system: str,
    user: str,
    response: Any,
    duration_ms: int,
    tokens: Optional[dict] = None,
    error: Optional[str] = None,
) -> str:
    """Full-content LLM trace for UI + LangSmith llm span."""
    from . import model_routing, metrics_registry

    response_str = (
        json.dumps(response, default=str) if isinstance(response, dict) else str(response)
    )
    tok = tokens or {}
    p_tok = int(tok.get("prompt") or 0)
    c_tok = int(tok.get("completion") or 0)
    est_cost = model_routing.estimate_cost_usd(model, p_tok, c_tok) if mode != "mock" else 0.0

    event = {
        "mode": mode,
        "purpose": purpose,
        "model": model,
        "system": system,
        "user": user,
        "response": response_str[:12000],
        "system_preview": system[:500],
        "user_preview": user[:2000],
        "response_preview": response_str[:2000],
        "tokens": tokens,
        "estimated_cost_usd": est_cost,
        "error": error,
    }
    metrics_registry.observe_llm_call(
        purpose=purpose,
        model=model,
        mode=mode,
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        cost_usd=est_cost,
        duration_ms=duration_ms,
    )
    local_id = record("llm_call", event, duration_ms=duration_ms)

    if langsmith_enabled() and not error:
        try:
            with ls_span(
                f"llm.{purpose}",
                run_type="llm",
                inputs={
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "model": model,
                    "purpose": purpose,
                },
                metadata={"model": model, "mode": mode},
                tags=["llm", mode],
            ) as ls_run:
                if ls_run is not None:
                    ls_run.end(outputs={
                        "response": response_str[:8000],
                        "usage_metadata": tokens,
                    })
        except Exception as e:
            print(f"[langsmith] llm span failed: {e}")

    return local_id


def record_tool_call(
    tool_name: str,
    args: tuple,
    kwargs: dict,
    result: Any,
    duration_ms: int,
    status: str = "ok",
    error: Optional[str] = None,
) -> str:
    """Tool trace with LangSmith tool span."""
    preview = _json_preview(result if status == "ok" else {"error": error})
    event = {
        "tool": tool_name,
        "args": [str(a)[:200] for a in args],
        "kwargs": {k: str(v)[:200] for k, v in kwargs.items()},
        "status": status,
        "result_preview": preview,
        "result": preview[:8000] if status == "ok" else None,
        "error": error,
    }
    local_id = record("tool_call", event, duration_ms=duration_ms)

    if langsmith_enabled():
        try:
            with ls_span(
                f"tool.{tool_name}",
                run_type="tool",
                inputs={"args": event["args"], "kwargs": event["kwargs"]},
                tags=["tool", tool_name],
            ) as ls_run:
                if ls_run is not None:
                    if status == "ok":
                        try:
                            out_payload = json.loads(preview)
                        except Exception:
                            out_payload = preview
                        ls_run.end(outputs={"result": out_payload})
                    else:
                        ls_run.end(error=error)
        except Exception:
            pass

    return local_id


def list_for_run(run_id: str) -> list[dict]:
    with sqlite3.connect(config.FIRM_DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM traces WHERE run_id=? ORDER BY ts ASC", (run_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["event_data"] = json.loads(d["event_data"]) if d["event_data"] else {}
            out.append(d)
        return out


def list_recent(
    *,
    since_ts: float = 0,
    limit: int = 400,
    run_id: Optional[str] = None,
) -> list[dict]:
    """Traces for the operations view — newest first, optional run filter."""
    q = "SELECT * FROM traces WHERE ts >= ?"
    params: list[Any] = [since_ts]
    if run_id:
        q += " AND run_id=?"
        params.append(run_id)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with sqlite3.connect(config.FIRM_DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["event_data"] = json.loads(d["event_data"]) if d["event_data"] else {}
            out.append(d)
        return out


def list_for_alert(alert_id: str, *, limit: int = 40) -> list[dict]:
    """Trace rows linked to an ops alert (ops_alert events)."""
    try:
        init_db()
        with sqlite3.connect(config.FIRM_DB) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM traces WHERE event_type='ops_alert' AND event_data LIKE ? "
                "ORDER BY ts DESC LIMIT ?",
                (f"%{alert_id}%", limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["event_data"] = json.loads(d["event_data"]) if d["event_data"] else {}
                out.append(d)
            return out
    except sqlite3.OperationalError:
        return []


def list_for_trading_day(limit: int = 500) -> list[dict]:
    """All traces since midnight ET today (for sorted trace explorer)."""
    from .market_calendar import ET, trading_date

    day = trading_date()
    start_et = datetime.combine(day, dt_time.min, tzinfo=ET)
    since = start_et.astimezone(timezone.utc).timestamp()
    rows = list_recent(since_ts=since, limit=limit)
    rows.reverse()  # chronological for timeline display
    return rows


def list_for_agent(run_id: str, agent: str) -> list[dict]:
    with sqlite3.connect(config.FIRM_DB) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM traces WHERE run_id=? AND agent=? ORDER BY ts ASC",
            (run_id, agent),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["event_data"] = json.loads(d["event_data"]) if d["event_data"] else {}
            out.append(d)
        return out


def tools_by_agent(run_id: str) -> dict[str, list[str]]:
    """Unique tool / RAG corpus names used per agent in this run."""
    out: dict[str, list[str]] = {}
    for t in list_for_run(run_id):
        agent = t.get("agent") or "_global"
        label: Optional[str] = None
        if t.get("event_type") == "tool_call":
            label = (t.get("event_data") or {}).get("tool")
        elif t.get("event_type") == "rag_retrieval":
            corpus = (t.get("event_data") or {}).get("corpus")
            if corpus:
                label = f"rag:{corpus}"
        elif t.get("event_type") == "llm_call":
            purpose = (t.get("event_data") or {}).get("purpose")
            if purpose:
                label = f"llm:{purpose}"
        if not label:
            continue
        bucket = out.setdefault(agent, [])
        if label not in bucket:
            bucket.append(label)
    return out


def _fmt_duration_ms(ms: float) -> str:
    ms = float(ms or 0)
    if ms < 1000:
        return f"{int(ms)} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _timeline_bar_label(t: dict) -> str:
    et = t.get("event_type") or "event"
    data = t.get("event_data") or {}
    if et == "tool_call":
        return str(data.get("tool") or "tool")
    if et == "rag_retrieval":
        return str(data.get("corpus") or "rag")
    if et == "llm_call":
        return str(data.get("purpose") or data.get("model") or "llm")
    if et in ("span_start", "span_end"):
        return str(data.get("name") or et)
    if t.get("agent"):
        return str(t["agent"])
    return et.replace("_", " ")


def enrich_waterfall_timeline(items: list[dict]) -> dict:
    """Add timeline bar geometry + DOM ids linking bars to waterfall rows."""
    if not items:
        return {"items": [], "total_ms": 1, "total_label": "0 ms"}

    start_ts = min(float(t["ts"]) for t in items)
    end_ts = start_ts
    for t in items:
        dur_s = float(t.get("duration_ms") or 0) / 1000.0
        if dur_s <= 0:
            dur_s = 0.02 if t.get("event_type") in (
                "agent_start", "agent_end", "span_start", "span_end",
            ) else 0.03
        end_ts = max(end_ts, float(t["ts"]) + dur_s)

    total_ms = max(1.0, (end_ts - start_ts) * 1000.0)
    enriched: list[dict] = []
    for i, raw in enumerate(items):
        t = dict(raw)
        offset_ms = max(0.0, (float(t["ts"]) - start_ts) * 1000.0)
        dur_ms = float(t.get("duration_ms") or 0)
        if dur_ms <= 0:
            dur_ms = 18.0 if t.get("event_type") in (
                "agent_start", "agent_end", "span_start", "span_end",
            ) else 24.0
        start_pct = min(99.2, (offset_ms / total_ms) * 100.0)
        width_pct = max(0.45, min(100.0 - start_pct, (dur_ms / total_ms) * 100.0))
        tid = t.get("trace_id") or f"idx{i}"
        t["timeline_index"] = i
        t["timeline_start_pct"] = round(start_pct, 3)
        t["timeline_width_pct"] = round(width_pct, 3)
        t["timeline_label"] = _timeline_bar_label(t)
        t["span_dom_id"] = f"trace-span-{tid}"
        enriched.append(t)

    return {
        "items": enriched,
        "total_ms": int(total_ms),
        "total_label": _fmt_duration_ms(total_ms),
    }


def build_waterfall_timeline(run_id: str) -> dict:
    """Waterfall rows plus timeline metadata for linked mini-Gantt UI."""
    items = build_waterfall(run_id)
    return enrich_waterfall_timeline(items)


_SKIP_PIPELINE_EVENTS = frozenset({
    "agent_end", "span_start", "span_end",
})


def _trace_step_title(t: dict) -> tuple[str, str, str]:
    """Return (kind, title, subtitle) for a trace row."""
    et = t.get("event_type") or "event"
    data = t.get("event_data") or {}
    if et == "llm_call":
        return (
            "llm",
            str(data.get("purpose") or "LLM call"),
            str(data.get("model") or data.get("mode") or ""),
        )
    if et == "tool_call":
        return (
            "tool",
            str(data.get("tool") or "tool"),
            str(data.get("status") or "ok"),
        )
    if et == "rag_retrieval":
        corpus = str(data.get("corpus") or "rag")
        hits = data.get("hits_count")
        sub = f"{hits} hits" if hits is not None else ""
        return ("rag", corpus, sub)
    if et == "agent_start":
        return ("agent", str(t.get("agent") or "agent"), "started")
    if et in ("run_start", "run_end"):
        return ("event", et.replace("_", " "), str(data.get("final_status") or data.get("trigger_type") or "")[:80])
    if et.startswith("hitl") or et in ("autonomous_execution", "run_pause"):
        return ("event", et.replace("_", " "), str(data)[:80])
    return ("event", et.replace("_", " "), str(t.get("agent") or ""))


def _trace_to_pipeline_step(t: dict, *, nested: bool = False) -> dict[str, Any]:
    kind, title, subtitle = _trace_step_title(t)
    dur = int(t.get("duration_ms") or 0)
    data = t.get("event_data") or {}
    err = bool(data.get("error")) or _step_has_error(t)
    return {
        "step_id": t.get("trace_id") or "",
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "duration_ms": dur,
        "duration_label": _fmt_duration_ms(dur) if dur else "—",
        "agent": t.get("agent") or "",
        "event_type": t.get("event_type") or "",
        "status": "error" if err else "ok",
        "nested": nested,
        "trace": t,
    }


def _step_has_error(t: dict) -> bool:
    data = t.get("event_data") or {}
    if t.get("event_type") == "tool_call":
        return str(data.get("status", "")).lower() in ("error", "failed")
    return False


def _inventory_entry_from_child(child: dict) -> dict[str, Any]:
    """One row in the agent call inventory (LLM / tool / RAG)."""
    trace = child.get("trace") or {}
    data = trace.get("event_data") or {}
    kind = child.get("kind") or ""
    if kind == "llm":
        label = str(data.get("purpose") or child.get("title") or "llm")
        detail = str(data.get("model") or data.get("mode") or child.get("subtitle") or "")
    elif kind == "tool":
        label = str(data.get("tool") or child.get("title") or "tool")
        detail = str(data.get("status") or child.get("subtitle") or "")
    elif kind == "rag":
        label = str(data.get("corpus") or child.get("title") or "rag")
        hits = data.get("hits_count")
        detail = f"{hits} hits" if hits is not None else str(child.get("subtitle") or "")
    else:
        label = str(child.get("title") or kind)
        detail = str(child.get("subtitle") or "")
    return {
        "kind": kind,
        "label": label,
        "detail": detail,
        "duration_label": child.get("duration_label") or "—",
        "status": child.get("status") or "ok",
    }


def _call_inventory_from_children(children: list[dict]) -> dict[str, list[dict[str, Any]]]:
    inv: dict[str, list[dict[str, Any]]] = {"llm": [], "tool": [], "rag": []}
    for child in children:
        kind = child.get("kind") or ""
        if kind not in inv:
            continue
        inv[kind].append(_inventory_entry_from_child(child))
    return inv


def build_call_inventory_from_traces(trace_rows: list[dict]) -> dict[str, list[dict[str, Any]]]:
    """Call inventory for journal rows (raw trace events)."""
    children: list[dict] = []
    for t in trace_rows:
        et = t.get("event_type") or ""
        if et in ("llm_call", "tool_call", "rag_retrieval"):
            children.append(_trace_to_pipeline_step(t, nested=True))
    return _call_inventory_from_children(children)


def _subtitle_from_call_inventory(inv: dict[str, list[dict[str, Any]]]) -> str:
    parts: list[str] = []
    if inv.get("llm"):
        n = len(inv["llm"])
        parts.append(f"{n} LLM" if n != 1 else "1 LLM")
    if inv.get("tool"):
        n = len(inv["tool"])
        parts.append(f"{n} tools" if n != 1 else "1 tool")
    if inv.get("rag"):
        n = len(inv["rag"])
        parts.append(f"{n} RAG" if n != 1 else "1 RAG")
    return " · ".join(parts) if parts else "no calls"


def _agent_title_with_context(agent: str, child_traces: list[dict]) -> str:
    base = agent.replace("_", " ")
    if agent != "plan_supervisor":
        return base
    for trace in child_traces:
        data = trace.get("event_data") or {}
        ticker = data.get("ticker")
        if ticker:
            return f"{base} · {ticker}"
        preview = str(data.get("user_preview") or data.get("user") or "")
        for token in ("ticker=", "Ticker ", "ticker "):
            if token.lower() in preview.lower():
                idx = preview.lower().find(token.lower())
                chunk = preview[idx + len(token): idx + len(token) + 12].strip(" :\"'=,")
                sym = "".join(c for c in chunk if c.isalnum() or c == ".")[:8]
                if sym:
                    return f"{base} · {sym.upper()}"
    return base


def build_trace_pipeline(run_id: str) -> dict[str, Any]:
    """
    Compact vertical pipeline: agent milestones with nested tool/LLM/RAG steps.
    Each step expands to full trace payload (prompts, args, hits).
    """
    rows = list_for_run(run_id)
    if not rows:
        return {"steps": [], "total_steps": 0, "total_label": "0 ms", "summary": "No trace events"}

    steps: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        t = rows[i]
        et = t.get("event_type") or ""

        if et in _SKIP_PIPELINE_EVENTS:
            i += 1
            continue

        if et == "agent_start":
            agent = t.get("agent") or "agent"
            children: list[dict[str, Any]] = []
            child_traces: list[dict] = []
            start_ts = float(t["ts"])
            i += 1
            total_dur = 0
            while i < len(rows) and rows[i].get("event_type") != "agent_end":
                child = rows[i]
                cet = child.get("event_type") or ""
                if cet not in _SKIP_PIPELINE_EVENTS:
                    children.append(_trace_to_pipeline_step(child, nested=True))
                    child_traces.append(child)
                    total_dur += int(child.get("duration_ms") or 0)
                i += 1
            if i < len(rows) and rows[i].get("event_type") == "agent_end":
                end_row = rows[i]
                total_dur = int(end_row.get("duration_ms") or 0) or int(
                    (float(end_row["ts"]) - start_ts) * 1000
                )
                i += 1
            tool_names = [
                c["title"] for c in children if c["kind"] == "tool"
            ][:6]
            call_inventory = _call_inventory_from_children(children)
            steps.append({
                "step_id": t.get("trace_id") or f"agent-{agent}-{len(steps)}",
                "kind": "agent",
                "title": _agent_title_with_context(agent, child_traces),
                "subtitle": _subtitle_from_call_inventory(call_inventory),
                "duration_ms": total_dur,
                "duration_label": _fmt_duration_ms(total_dur) if total_dur else "—",
                "agent": agent,
                "event_type": "agent",
                "status": "ok",
                "nested": False,
                "children": children,
                "call_inventory": call_inventory,
                "tools": tool_names,
                "trace": t,
            })
            continue

        steps.append(_trace_to_pipeline_step(t))
        i += 1

    total_ms = sum(int(s.get("duration_ms") or 0) for s in steps)
    kinds: dict[str, int] = {}
    for s in steps:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        for c in s.get("children") or []:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    parts: list[str] = []
    if kinds.get("agent"):
        parts.append(f"{kinds['agent']} agents")
    if kinds.get("llm"):
        parts.append(f"{kinds['llm']} LLM")
    if kinds.get("tool"):
        parts.append(f"{kinds['tool']} tools")
    if kinds.get("rag"):
        parts.append(f"{kinds['rag']} RAG")
    if kinds.get("event"):
        parts.append(f"{kinds['event']} events")
    summary = " · ".join(parts)

    return {
        "steps": steps,
        "total_steps": len(steps),
        "total_ms": total_ms,
        "total_label": _fmt_duration_ms(total_ms),
        "summary": summary or f"{len(steps)} steps",
    }


def build_waterfall(run_id: str) -> list[dict]:
    """Flatten traces into display order with indentation depth."""
    traces = list_for_run(run_id)
    by_parent: dict[Optional[str], list[dict]] = {}
    for t in traces:
        by_parent.setdefault(t.get("parent_trace_id"), []).append(t)

    def walk(parent_id: Optional[str], depth: int) -> list[dict]:
        out: list[dict] = []
        for t in by_parent.get(parent_id, []):
            t = dict(t)
            t["depth"] = depth
            out.append(t)
            out.extend(walk(t["trace_id"], depth + 1))
        return out

    roots = [t for t in traces if not t.get("parent_trace_id")]
    if not roots:
        return [{**t, "depth": 0} for t in traces]
    result: list[dict] = []
    for r in sorted(roots, key=lambda x: x["ts"]):
        result.append({**r, "depth": 0})
        result.extend(walk(r["trace_id"], 1))
    return result


# -------- LangSmith integration --------

_langsmith_client = None
_langsmith_checked = False


def langsmith_enabled() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY")
                or os.environ.get("LANGCHAIN_API_KEY"))


def _get_langsmith():
    global _langsmith_client, _langsmith_checked
    if _langsmith_checked:
        return _langsmith_client
    _langsmith_checked = True
    if not langsmith_enabled():
        return None
    try:
        from langsmith import Client
        _langsmith_client = Client()
    except Exception as e:
        print(f"[langsmith] init failed: {e}")
        _langsmith_client = None
    return _langsmith_client


def langsmith_traceable(name: str, run_type: str = "chain"):
    """Decorator: root chain run in LangSmith with horizon metadata."""
    def decorator(fn):
        if not langsmith_enabled():
            return fn
        try:
            from langsmith import traceable

            @traceable(
                name=name,
                run_type=run_type,
                metadata={"app": "horizon-capital"},
            )
            def wrapped(*args, **kwargs):
                return fn(*args, **kwargs)

            return wrapped
        except Exception:
            return fn
    return decorator


def env_status() -> dict:
    cfg = configure_langsmith() if langsmith_enabled() else {}
    return {
        "langsmith_api_key_set": langsmith_enabled(),
        "langsmith_project": os.environ.get(
            "LANGSMITH_PROJECT",
            os.environ.get("LANGCHAIN_PROJECT", "horizon-capital")),
        "langsmith_tracing": os.environ.get(
            "LANGSMITH_TRACING",
            os.environ.get("LANGCHAIN_TRACING_V2", "false")),
        "langchain_tracing_v2": os.environ.get("LANGCHAIN_TRACING_V2", "false"),
        "langsmith_client_ok": _get_langsmith() is not None,
        "startup_config": cfg,
    }
