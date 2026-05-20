"""Operator exit from open positions."""
from __future__ import annotations

from app import config, db, tools


def test_close_position_full_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(config, "STARTING_NAV", 1_000_000.0)
    db.init_db()

    db.upsert_holding("TEST", 100, 50.0, 55.0, "plan_test1", "Information Technology")
    db.save_plan("plan_test1", "TEST", "active", {"id": "plan_test1", "ticker": "TEST"})

    out = tools.close_position_sim("TEST", run_id="test_exit")
    assert out["status"] == "filled"
    assert out["full_exit"] is True
    assert db.list_holdings() == []

    row = db.get_plan("plan_test1")
    assert row["status"] == "closed"
