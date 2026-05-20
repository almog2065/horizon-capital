"""Firm timeline builder for dashboard charts."""
from __future__ import annotations

import json
import time

from app import config, db, firm_timeline


def test_build_firm_timeline_from_balance_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    db.init_db()

    ts = time.time() - 3600
    state = {
        "firm_state": {
            "nav_usd": 1_020_000,
            "cash_pct": 0.12,
            "invested_pct": 0.88,
            "positions_count": 14,
            "trading_posture": {"label": "balanced"},
        },
        "final_status": "completed_firm_balance",
    }
    db.save_run(
        "bal_test1",
        "firm_balance",
        {},
        "2026-05-19T10:00:00",
        "completed",
        state,
    )
    with db.conn() as c:
        c.execute(
            "UPDATE runs SET created_at=? WHERE run_id=?",
            (ts, "bal_test1"),
        )
        c.commit()

    tl = firm_timeline.build_firm_timeline(max_runs=10, include_current=False)
    assert tl["has_data"]
    assert len(tl["metrics"]) >= 1
    assert tl["metrics"][-1]["nav_usd"] == 1_020_000
    assert any(e["kind"] == "run" for e in tl["events"])
