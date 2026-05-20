"""CLI: render today's daily report.

Usage:
    python -m app.reports               # writes to artifacts/reports/<date>/daily.xlsx
    python -m app.reports --window 2026-05-19 --out /tmp/report.xlsx
    python -m app.reports --demo        # synthetic data (no firm state needed)

Returns 0 on success, prints the written path to stdout.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..core.logging import get_logger, setup_logging

log = get_logger("horizon.reports.cli")


def _gather_demo_data() -> dict:
    return {
        "starting_nav": 1_000_000.0,
        "nav": 1_005_500.0,
        "benchmark_pct": 0.45,
        "holdings": [
            {"ticker": "MSFT", "qty": 100, "avg_cost": 410.0, "last_price": 415.5},
            {"ticker": "NVDA", "qty": 50,  "avg_cost": 880.0, "last_price": 892.3},
            {"ticker": "AAPL", "qty": 75,  "avg_cost": 192.0, "last_price": 195.2},
        ],
        "trades": [
            {
                "ts": "2026-05-19T09:31:00Z", "ticker": "MSFT", "side": "buy",
                "qty": 100, "price": 410.0,
                "citations": ["10K-2024:p27", "EDGAR:MSFT-2025-Q1"], "hitl": False,
            },
            {
                "ts": "2026-05-19T10:02:00Z", "ticker": "NVDA", "side": "buy",
                "qty": 50, "price": 880.0,
                "citations": ["news:nvda-guidance-2025-05-15"], "hitl": True,
            },
            {
                "ts": "2026-05-19T14:45:00Z", "ticker": "MSFT", "side": "sell",
                "qty": 100, "price": 415.5, "realized_pnl": 550.0,
                "citations": ["plan:msft-2025"], "hitl": False,
            },
        ],
    }


def _gather_live_data() -> dict:
    """Pull the same data the API route uses. Falls back to demo data on failure."""
    try:
        from .. import db, firm_state, trade_history  # type: ignore

        firm = firm_state.build_firm_state(refresh_prices=False)
        return {
            "starting_nav": float(firm.get("starting_nav", 1_000_000.0)),
            "nav": float(firm.get("nav", 1_000_000.0)),
            "benchmark_pct": 0.0,
            "holdings": [
                {
                    "ticker": h.get("ticker"),
                    "qty": h.get("qty", 0),
                    "avg_cost": h.get("avg_cost", 0),
                    "last_price": h.get("last_price", h.get("avg_cost", 0)),
                }
                for h in db.list_holdings()
            ],
            "trades": [
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
                for t in trade_history.get_firm_trade_history(limit=200)
            ],
        }
    except Exception as e:
        log.warning("live data unavailable (%s) — using demo data", e)
        return _gather_demo_data()


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(description="Render the firm's daily Excel report")
    p.add_argument("--window", default=None, help="Date label for the report (default: today UTC)")
    p.add_argument("--out", default=None, help="Output path. Default: artifacts/reports/<date>/daily.xlsx")
    p.add_argument("--demo", action="store_true", help="Use synthetic data (no firm state required)")
    args = p.parse_args(argv)

    from . import build_daily_report, write_daily_report_xlsx

    data = _gather_demo_data() if args.demo else _gather_live_data()
    window = args.window or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rep = build_daily_report(**data, window=window)
    out_path = Path(args.out) if args.out else None
    written = write_daily_report_xlsx(rep, out_path)
    print(written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
