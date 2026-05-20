"""Operational alerts — capture, persist, and expose firm errors."""
from __future__ import annotations

from typing import Any, Literal, Optional

from . import ops_db
from .core.logging import get_logger

log = get_logger("horizon.alerts")

Severity = Literal["info", "warning", "error", "critical"]


def record(
    *,
    code: str,
    message: str,
    severity: Severity = "error",
    source: str = "app",
    context: Optional[dict] = None,
    run_id: Optional[str] = None,
) -> dict:
    """Append an alert and emit structured log."""
    ops_db.init_db()
    alert = ops_db.insert_alert(
        code=code,
        message=message,
        severity=severity,
        source=source,
        context=context,
        run_id=run_id,
    )

    log.log(
        40 if severity == "critical" else 30 if severity == "error" else 20,
        "ops-alert code=%s severity=%s source=%s msg=%s",
        code,
        severity,
        source,
        message[:500],
        extra={
            "event": "ops_alert",
            "alert_id": alert["alert_id"],
            "alert_code": code,
            "alert_severity": severity,
            "alert_source": source,
        },
    )

    try:
        from . import metrics_registry
        metrics_registry.inc(
            "horizon_alerts_total",
            severity=severity,
            code=code[:40],
        )
    except Exception:
        pass

    try:
        from . import traces
        traces.record("ops_alert", {
            "alert_id": alert["alert_id"],
            "code": code,
            "severity": severity,
            "message": message[:500],
            "source": source,
        })
    except Exception:
        pass

    return alert


def get_alert(alert_id: str) -> Optional[dict]:
    ops_db.init_db()
    return ops_db.get_alert(alert_id)


def list_alerts(
    *,
    limit: int = 40,
    unacked_only: bool = False,
    min_severity: Optional[Severity] = None,
    severity: Optional[Severity] = None,
    code: Optional[str] = None,
) -> list[dict]:
    ops_db.init_db()
    return ops_db.list_alerts(
        limit=limit,
        unacked_only=unacked_only,
        min_severity=min_severity,
        severity=severity,
        code=code,
    )


def distinct_codes(limit: int = 30) -> list[str]:
    ops_db.init_db()
    return ops_db.distinct_alert_codes(limit=limit)


def acknowledge(alert_id: str) -> bool:
    ops_db.init_db()
    return ops_db.acknowledge_alert(alert_id)


def acknowledge_all() -> int:
    """Ack all open alerts. Returns count closed."""
    ops_db.init_db()
    return ops_db.acknowledge_all_open()


def summary() -> dict[str, Any]:
    ops_db.init_db()
    return ops_db.alerts_summary()


def format_user_message(alert: dict) -> str:
    return f"[{alert.get('severity', 'error').upper()}] {alert.get('code')}: {alert.get('message', '')}"
