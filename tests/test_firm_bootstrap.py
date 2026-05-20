"""Firm bootstrap — policy-aligned initial book."""
from __future__ import annotations

from app import allocation, config, db, firm_bootstrap, firm_state


def test_bootstrap_table_targets_small_gap_vs_policy():
    snap = firm_bootstrap.bootstrap_policy_snapshot()
    lo, hi = allocation.TARGET_POSITION_COUNT
    assert lo <= snap["positions"] <= hi
    assert 0.82 <= snap["invested_pct"] <= 0.845
    assert 0.155 <= snap["cash_pct"] <= 0.18
    it_pct = snap["sector_pct"]["Information Technology"]
    it_target = allocation.SECTOR_TARGETS["Information Technology"]["target"]
    assert it_pct < it_target
    assert it_pct >= it_target * 0.80


def test_bootstrap_seeds_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(config, "STARTING_NAV", 1_000_000.0)
    db.init_db()

    out = firm_bootstrap.ensure_balanced_book()
    assert out["seeded"] is True
    lo, hi = allocation.TARGET_POSITION_COUNT
    assert lo <= out["positions"] <= hi
    assert out["within_position_count_band"] is True
    assert 0.80 <= out["invested_pct"] <= 0.86
    assert out["digital_assets_pct"] <= allocation.MAX_DIGITAL_ASSETS_PCT
    assert len(db.list_holdings()) == out["positions"]
    assert db.list_plans(status="active")

    again = firm_bootstrap.ensure_balanced_book()
    assert again["seeded"] is False


def test_bootstrap_starts_balanced_posture(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIRM_DB", tmp_path / "firm.sqlite")
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(config, "STARTING_NAV", 1_000_000.0)
    db.init_db()
    firm_bootstrap.ensure_balanced_book()

    fs = firm_state.build_firm_state(refresh_prices=False)
    deploy = fs["deployment_needs"]
    assert deploy["need_deploy"] is False
    assert deploy["need_diversify"] is False
    assert deploy["active"] is False
    assert fs["trading_posture"]["mode"] == "balanced"
    assert fs["invested_pct"] >= allocation.MIN_INVESTED_PCT
    assert fs["cash_pct"] <= allocation.CASH_CEILING_PCT
    for p in fs["positions"]:
        assert p["pct_nav"] <= allocation.MAX_POSITION_PCT + 0.01
