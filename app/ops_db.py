"""Operations persistence — alerts, daily plans, runtime dossiers.

Separate SQLite from firm business state (`firm.sqlite`) and RAG (`vectors.sqlite`).
Survives container restarts via the same artifacts volume / local `artifacts/` dir.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from . import config
from .core.logging import get_logger

log = get_logger("horizon.ops_db")

Severity = Literal["info", "warning", "error", "critical"]
_MAX_ALERTS = 200


SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_alerts (
    alert_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL,
    source TEXT NOT NULL,
    run_id TEXT,
    context_json TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    acked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ops_alerts_ts ON ops_alerts(ts DESC);

CREATE TABLE IF NOT EXISTS daily_plans (
    plan_date TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dossiers (
    ticker TEXT PRIMARY KEY,
    dossier_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'runtime',
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bootstrap_state (
    key TEXT PRIMARY KEY,
    completed_at REAL NOT NULL,
    detail_json TEXT
);
"""


@contextmanager
def conn():
    config.OPS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.OPS_DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def bootstrap_done(key: str) -> bool:
    with conn() as c:
        row = c.execute(
            "SELECT 1 FROM bootstrap_state WHERE key = ?", (key,)
        ).fetchone()
    return row is not None


def mark_bootstrap_done(key: str, detail: Optional[dict] = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO bootstrap_state (key, completed_at, detail_json) "
            "VALUES (?, ?, ?)",
            (key, time.time(), json.dumps(detail or {}, default=str)),
        )


# ---------- ops alerts ----------


def insert_alert(
    *,
    code: str,
    message: str,
    severity: Severity = "error",
    source: str = "app",
    context: Optional[dict] = None,
    run_id: Optional[str] = None,
) -> dict:
    alert = {
        "alert_id": uuid.uuid4().hex[:12],
        "ts": time.time(),
        "code": code,
        "message": message[:2000],
        "severity": severity,
        "source": source,
        "run_id": run_id,
        "context": context or {},
        "acknowledged": False,
    }
    with conn() as c:
        c.execute(
            "INSERT INTO ops_alerts "
            "(alert_id, ts, code, message, severity, source, run_id, context_json, acknowledged) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                alert["alert_id"],
                alert["ts"],
                alert["code"],
                alert["message"],
                alert["severity"],
                alert["source"],
                alert["run_id"],
                json.dumps(alert["context"], default=str),
            ),
        )
        _trim_alerts(c)
    return alert


def _trim_alerts(c: sqlite3.Connection) -> None:
    n = c.execute("SELECT COUNT(*) FROM ops_alerts").fetchone()[0]
    if n <= _MAX_ALERTS:
        return
    excess = n - _MAX_ALERTS
    c.execute(
        "DELETE FROM ops_alerts WHERE alert_id IN ("
        "SELECT alert_id FROM ops_alerts ORDER BY ts ASC LIMIT ?"
        ")",
        (excess,),
    )


def _row_to_alert(row: sqlite3.Row) -> dict:
    d = dict(row)
    ctx = d.pop("context_json", None)
    d["context"] = json.loads(ctx) if ctx else {}
    d["acknowledged"] = bool(d.get("acknowledged"))
    return d


def get_alert(alert_id: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute(
            "SELECT * FROM ops_alerts WHERE alert_id = ?", (alert_id,)
        ).fetchone()
    return _row_to_alert(row) if row else None


def list_alerts(
    *,
    limit: int = 40,
    unacked_only: bool = False,
    min_severity: Optional[Severity] = None,
    severity: Optional[Severity] = None,
    code: Optional[str] = None,
) -> list[dict]:
    severity_rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    min_rank = severity_rank.get(min_severity or "info", 0)
    q = "SELECT * FROM ops_alerts ORDER BY ts DESC"
    with conn() as c:
        rows = c.execute(q).fetchall()
    out: list[dict] = []
    for row in rows:
        a = _row_to_alert(row)
        if unacked_only and a.get("acknowledged"):
            continue
        if severity and a.get("severity") != severity:
            continue
        if code and a.get("code") != code:
            continue
        if severity_rank.get(a.get("severity", "info"), 0) < min_rank:
            continue
        out.append(a)
        if len(out) >= limit:
            break
    return out


def distinct_alert_codes(limit: int = 30) -> list[str]:
    seen: list[str] = []
    for a in list_alerts(limit=500):
        c = a.get("code")
        if c and c not in seen:
            seen.append(str(c))
        if len(seen) >= limit:
            break
    return seen


def acknowledge_alert(alert_id: str) -> bool:
    with conn() as c:
        cur = c.execute(
            "UPDATE ops_alerts SET acknowledged = 1, acked_at = ? "
            "WHERE alert_id = ? AND acknowledged = 0",
            (time.time(), alert_id),
        )
        return cur.rowcount > 0


def acknowledge_all_open() -> int:
    """Mark every unacknowledged alert as acked. Returns rows updated."""
    with conn() as c:
        cur = c.execute(
            "UPDATE ops_alerts SET acknowledged = 1, acked_at = ? "
            "WHERE acknowledged = 0",
            (time.time(),),
        )
        return int(cur.rowcount)


def alerts_summary() -> dict[str, Any]:
    with conn() as c:
        rows = c.execute("SELECT * FROM ops_alerts").fetchall()
    alerts = [_row_to_alert(r) for r in rows]
    open_alerts = [a for a in alerts if not a.get("acknowledged")]
    by_sev: dict[str, int] = {}
    for a in open_alerts:
        s = a.get("severity", "info")
        by_sev[s] = by_sev.get(s, 0) + 1
    latest = open_alerts[-5:] if open_alerts else []
    return {
        "total_stored": len(alerts),
        "open": len(open_alerts),
        "by_severity": by_sev,
        "latest": latest,
    }


# ---------- daily plans ----------


def load_daily_plan(plan_date: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute(
            "SELECT plan_json FROM daily_plans WHERE plan_date = ?", (plan_date,)
        ).fetchone()
    if not row:
        return None
    return json.loads(row["plan_json"])


def save_daily_plan(plan_date: str, plan: dict) -> None:
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO daily_plans (plan_date, plan_json, updated_at) "
            "VALUES (?, ?, ?)",
            (plan_date, json.dumps(plan, default=str), time.time()),
        )


def plan_storage_ref(plan_date: str) -> Path:
    """Logical location for logs/UI (actual store is OPS_DB)."""
    return config.OPS_DB.parent / "ops.sqlite" / f"daily_plans/{plan_date}"


# ---------- dossiers ----------


def get_dossier(ticker: str) -> Optional[dict]:
    with conn() as c:
        row = c.execute(
            "SELECT dossier_json, source FROM dossiers WHERE ticker = ?",
            (ticker.upper(),),
        ).fetchone()
    if not row:
        return None
    return {
        "dossier": json.loads(row["dossier_json"]),
        "source": row["source"],
    }


def upsert_dossier(ticker: str, dossier: dict, *, source: str = "runtime") -> str:
    t = ticker.upper()
    payload = {**dossier, "ticker": t}
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dossiers (ticker, dossier_json, source, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (t, json.dumps(payload, indent=2), source, time.time()),
        )
    return t


def list_dossier_tickers() -> list[str]:
    with conn() as c:
        rows = c.execute("SELECT ticker FROM dossiers ORDER BY ticker").fetchall()
    return [r["ticker"] for r in rows]


def count_dossiers() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM dossiers").fetchone()[0]


# ---------- legacy JSON migration ----------


def migrate_legacy_json_artifacts() -> dict[str, Any]:
    """One-time import from artifacts/operations/*.json (pre-ops_db)."""
    init_db()
    migrated: dict[str, Any] = {"alerts": 0, "daily_plans": 0}

    legacy_alerts = config.ARTIFACTS / "operations" / "ops_alerts.json"
    if legacy_alerts.exists():
        try:
            data = json.loads(legacy_alerts.read_text(encoding="utf-8"))
            for a in data.get("alerts") or []:
                with conn() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO ops_alerts "
                        "(alert_id, ts, code, message, severity, source, run_id, "
                        "context_json, acknowledged, acked_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            a.get("alert_id"),
                            a.get("ts", time.time()),
                            a.get("code", "legacy"),
                            (a.get("message") or "")[:2000],
                            a.get("severity", "error"),
                            a.get("source", "app"),
                            a.get("run_id"),
                            json.dumps(a.get("context") or {}, default=str),
                            1 if a.get("acknowledged") else 0,
                            a.get("acked_at"),
                        ),
                    )
                migrated["alerts"] += 1
            backup = legacy_alerts.with_suffix(".json.migrated")
            legacy_alerts.rename(backup)
            log.info("migrated ops_alerts.json -> ops.sqlite (%d rows)", migrated["alerts"])
        except Exception as e:
            log.warning("ops_alerts migration failed: %s", e)

    ops_dir = config.ARTIFACTS / "operations"
    if ops_dir.exists():
        for path in ops_dir.glob("daily_plan_*.json"):
            try:
                plan = json.loads(path.read_text(encoding="utf-8"))
                day = plan.get("date") or path.stem.replace("daily_plan_", "")
                save_daily_plan(day, plan)
                path.rename(path.with_suffix(".json.migrated"))
                migrated["daily_plans"] += 1
            except Exception as e:
                log.warning("daily_plan migration %s: %s", path, e)

    return migrated
