"""Report endpoints — exposes the daily report as a downloadable Excel file.

URL    Method  Returns
---------------------------------------------------------------
/reports/daily.xlsx     GET    application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
/reports/daily.json     GET    application/json   (same data, machine-friendly)

The report is built **on demand** from the firm state at request time.
For large firms / many tenants, push this behind a queue and serve a
pre-rendered file from S3 — wire that via Redis in a follow-up.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from ...core.logging import get_logger

log = get_logger("horizon.api.reports")
router = APIRouter(prefix="/reports", tags=["reports"])


def _build_today() -> "object":
    """Pull state and produce a DailyReport. Falls back to a synthetic snapshot
    if the legacy modules aren't initialised yet (e.g., first request after
    boot in a clean dev DB)."""
    from datetime import datetime, timezone

    from ...reports import build_daily_report

    starting_nav = 1_000_000.0
    nav = starting_nav
    holdings: list[dict] = []
    trades: list[dict] = []

    try:
        from ... import db, firm_state, trade_history

        firm = firm_state.build_firm_state(refresh_prices=False)
        nav = float(firm.get("nav", nav))
        starting_nav = float(firm.get("starting_nav", starting_nav))

        holdings = [
            {
                "ticker": h.get("ticker"),
                "qty": h.get("qty", 0),
                "avg_cost": h.get("avg_cost", 0),
                "last_price": h.get("last_price", h.get("avg_cost", 0)),
            }
            for h in db.list_holdings()
        ]
        th = trade_history.get_firm_trade_history(limit=200)
        trades = [
            {
                "ts": t.get("ts"),
                "ticker": t.get("ticker"),
                "side": t.get("side"),
                "qty": t.get("qty"),
                "price": t.get("price"),
                "realized_pnl": t.get("realized_pnl"),
                "hitl": bool(t.get("hitl")),
                "citations": t.get("citations") or [],
            }
            for t in th
        ]
    except Exception as e:  # pragma: no cover - first-boot fallback
        log.warning("report-fallback firm_state-not-ready err=%s", e)

    return build_daily_report(
        starting_nav=starting_nav,
        nav=nav,
        benchmark_pct=0.0,
        holdings=holdings,
        trades=trades,
        window=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    )


@router.get("/daily.xlsx")
def daily_xlsx() -> FileResponse:
    from ...reports import write_daily_report_xlsx
    rep = _build_today()
    path = write_daily_report_xlsx(rep)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.get("/daily.json")
def daily_json() -> JSONResponse:
    rep = _build_today()
    return JSONResponse(rep.as_dict())
