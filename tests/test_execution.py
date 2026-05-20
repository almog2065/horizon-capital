"""Execution-layer tests — pure functions, deterministic, no I/O.

Verifies the brief requirement: realistic fills with slippage,
commission, and market-hours awareness.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from app.execution import (
    Fill,
    asset_class_for,
    commission_for,
    compute_fill,
    is_buy,
    is_market_open,
    slippage_bp_for,
)


# -------- side helpers --------
def test_is_buy_accepts_aliases():
    assert is_buy("buy") is True
    assert is_buy("long") is True
    assert is_buy("sell") is False
    assert is_buy("short") is False


# -------- per-asset-class slippage --------
def test_slippage_bp_per_class():
    assert slippage_bp_for("equity") == 5
    assert slippage_bp_for("crypto") == 15
    assert slippage_bp_for("commodity_proxy") == 3
    assert slippage_bp_for("rates_proxy") == 2
    assert slippage_bp_for("fx_proxy") == 2
    # unknown falls back to equity
    assert slippage_bp_for("nonsense") == 5  # type: ignore[arg-type]


# -------- commission schedules --------
def test_commission_equity_flat():
    assert commission_for("equity", 100, 50.0) == pytest.approx(1.00)


def test_commission_etf_flat():
    assert commission_for("commodity_proxy", 50, 200.0) == pytest.approx(0.50)
    assert commission_for("rates_proxy", 50, 200.0) == pytest.approx(0.50)


def test_commission_crypto_bp_of_notional():
    # 10 bp on $100 = $0.10
    assert commission_for("crypto", 1, 100.0) == pytest.approx(0.10)
    # 10 bp on $50k = $50
    assert commission_for("crypto", 100, 500.0) == pytest.approx(50.0)


# -------- compute_fill: slippage direction & math --------
def test_compute_fill_buy_lifts_offer():
    f = compute_fill("long", 10, 100.0, asset_class="equity")
    # 5 bp = 0.05% — fill higher than last for a buy
    assert f.fill_price > 100.0
    assert math.isclose(f.fill_price, 100.05, abs_tol=1e-6)


def test_compute_fill_sell_hits_bid():
    f = compute_fill("sell", 10, 100.0, asset_class="equity")
    assert f.fill_price < 100.0
    assert math.isclose(f.fill_price, 99.95, abs_tol=1e-6)


def test_compute_fill_crypto_wider_slippage():
    eq = compute_fill("long", 1, 100.0, asset_class="equity")
    cr = compute_fill("long", 1, 100.0, asset_class="crypto")
    # Crypto worse fill than equity
    assert cr.fill_price > eq.fill_price


def test_compute_fill_notional_matches():
    f = compute_fill("long", 50, 200.0, asset_class="equity")
    assert math.isclose(f.notional_usd, 50 * f.fill_price, abs_tol=1e-6)


def test_compute_fill_commission_recorded():
    f = compute_fill("long", 100, 50.0, asset_class="equity")
    assert f.commission_usd == pytest.approx(1.00)


def test_compute_fill_invalid_quantity_raises():
    with pytest.raises(ValueError):
        compute_fill("long", 0, 100.0)
    with pytest.raises(ValueError):
        compute_fill("long", -5, 100.0)


def test_compute_fill_invalid_price_raises():
    with pytest.raises(ValueError):
        compute_fill("long", 10, 0.0)
    with pytest.raises(ValueError):
        compute_fill("long", 10, -100.0)


def test_compute_fill_rejects_nan_and_inf():
    # Regression: a bad market-data feed must not produce NaN fills.
    with pytest.raises(ValueError):
        compute_fill("long", 10, float("nan"))
    with pytest.raises(ValueError):
        compute_fill("long", 10, float("inf"))
    with pytest.raises(ValueError):
        compute_fill("long", 10, float("-inf"))


# -------- market hours --------
def test_market_open_equity_during_us_hours():
    # 14:30 UTC on a Wednesday — clearly inside 13:30–20:00 UTC window
    t = datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc)
    assert is_market_open("equity", now=t) is True


def test_market_closed_equity_weekend():
    # Saturday at 14:30 UTC
    t = datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc)
    assert is_market_open("equity", now=t) is False


def test_market_closed_equity_after_hours():
    # 22:00 UTC on a Wednesday — after 20:00 UTC close
    t = datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc)
    assert is_market_open("equity", now=t) is False


def test_market_always_open_crypto():
    # any time, including weekend
    sat_3am = datetime(2026, 5, 23, 3, 0, tzinfo=timezone.utc)
    assert is_market_open("crypto", now=sat_3am) is True


def test_fill_has_note_when_market_closed():
    sat = datetime(2026, 5, 23, 14, 30, tzinfo=timezone.utc)
    f = compute_fill("long", 10, 100.0, asset_class="equity", now=sat)
    assert f.market_hours_ok is False
    assert any("market hours" in n for n in f.notes)


def test_fill_no_note_when_market_open():
    weekday = datetime(2026, 5, 20, 14, 30, tzinfo=timezone.utc)
    f = compute_fill("long", 10, 100.0, asset_class="equity", now=weekday)
    assert f.market_hours_ok is True
    assert f.notes == []


# -------- asset_class_for resolves through asset_universe --------
def test_asset_class_for_unknown_falls_back_to_equity():
    # An obviously-unknown ticker shouldn't crash; just equity-default.
    assert asset_class_for("ZZZZZ_NOT_A_REAL_TICKER") == "equity"


def test_fill_dataclass_serialisable():
    f = compute_fill("long", 10, 100.0, asset_class="equity")
    d = f.as_dict()
    for k in ("fill_price", "notional_usd", "commission_usd", "slippage_bp",
              "market_hours_ok", "asset_class", "notes"):
        assert k in d
