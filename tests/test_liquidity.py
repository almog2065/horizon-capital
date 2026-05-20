"""Liquidity budget and simulate_order cash enforcement."""
from __future__ import annotations

from app import allocation, config, db, tools


def test_liquidity_budget_maiden_preserves_cash_target():
    nav = 1_000_000.0
    # 7% cash — below 8% target
    liq = allocation.liquidity_budget(nav, 70_000, maiden_entry=True)
    assert liq["status"] == "below_cash_target"
    assert liq["deployable_cash_usd"] == 0
    assert liq["can_open_new_name"] is False


def test_liquidity_budget_healthy_book():
    nav = 1_000_000.0
    liq = allocation.liquidity_budget(nav, 150_000, maiden_entry=True)
    assert liq["deployable_cash_usd"] == 70_000  # 15% - 8% reserve
    assert liq["max_new_maiden_entries"] == 2  # 70k / 30k


def test_simulate_order_blocks_when_deployable_exceeded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(config, "STARTING_NAV", 1_000_000.0)
    db.init_db()
    # Fully invested book
    db.upsert_holding("MSFT", 2000, 500.0, 500.0, "plan_x", "Information Technology")

    sim = tools.simulate_order("AAPL", "long", 500, 200.0)
    assert sim["feasible"] is False
    assert any(
        "deployable cash" in v["reason"].lower()
        for v in sim["policy_violations"]
    )
