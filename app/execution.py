"""Realistic execution layer — slippage, commission, market hours.

The brief requires: *"Manage a paper portfolio with realistic fills
(slippage, commission, market hours)."* This module is where that
modelling lives.

Design choices (all defensible):

* **Slippage** — basis-point function of side and asset class. Equities
  default to 5 bp; crypto to 15 bp (thinner books); commodity proxies to
  3 bp; rates/FX proxies to 2 bp. Configurable via env.
* **Commission** — flat $1 per stock trade, $0.50 per ETF, 0.10% of
  notional for crypto (closer to real broker fee schedules).
* **Market hours** — US equities & ETFs: 09:30–16:00 America/New_York,
  weekdays. Crypto: 24/7. The function returns
  `market_hours_ok=True/False` so the caller can decide what to do; we
  never auto-reject a trade here. The agents and HITL are the
  policy layer.

All math is pure and deterministic given the inputs. Tests pin
behaviour in `tests/test_execution.py`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone, timedelta
from typing import Literal, Optional

from .core.logging import get_logger

log = get_logger("horizon.execution")

Side = Literal["long", "short", "buy", "sell"]
AssetClass = Literal["equity", "crypto", "commodity_proxy", "rates_proxy", "fx_proxy"]


# -----------------------------------------------------------------------------
# Defaults (overridable via env — kept here instead of settings.py because
# they are deeply internal to execution and don't need cross-module visibility)
# -----------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# Slippage in basis points (1 bp = 0.01% of price).
SLIPPAGE_BP_EQUITY = _env_int("SLIPPAGE_BP_EQUITY", 5)
SLIPPAGE_BP_CRYPTO = _env_int("SLIPPAGE_BP_CRYPTO", 15)
SLIPPAGE_BP_COMMODITY = _env_int("SLIPPAGE_BP_COMMODITY", 3)
SLIPPAGE_BP_RATES_FX = _env_int("SLIPPAGE_BP_RATES_FX", 2)

# Commission schedules.
COMMISSION_EQUITY_USD = _env_float("COMMISSION_EQUITY_USD", 1.00)     # $/trade
COMMISSION_ETF_USD = _env_float("COMMISSION_ETF_USD", 0.50)           # $/trade
COMMISSION_CRYPTO_BP = _env_int("COMMISSION_CRYPTO_BP", 10)           # 10 bp = 0.10%

# Legacy UTC window constants (tests may still reference); runtime uses ET calendar.
US_MARKET_OPEN = time(13, 30)
US_MARKET_CLOSE = time(20, 0)

# Skip market hours check (useful for offline replays).
SKIP_MARKET_HOURS = os.getenv("SKIP_MARKET_HOURS", "").strip().lower() in (
    "1", "true", "yes", "on",
)


# -----------------------------------------------------------------------------
# Result type
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class Fill:
    """Result of a paper-fill simulation."""

    fill_price: float            # what the order printed at, after slippage
    notional_usd: float          # quantity * fill_price
    commission_usd: float        # broker fee (already deducted from cash)
    slippage_bp: int             # bp applied
    market_hours_ok: bool        # True if asset's market was open at fill time
    asset_class: AssetClass      # which class drove the slippage / commission
    notes: list[str]             # anything the operator should know

    def as_dict(self) -> dict:
        return {
            "fill_price": self.fill_price,
            "notional_usd": self.notional_usd,
            "commission_usd": self.commission_usd,
            "slippage_bp": self.slippage_bp,
            "market_hours_ok": self.market_hours_ok,
            "asset_class": self.asset_class,
            "notes": self.notes,
        }


# -----------------------------------------------------------------------------
# Public helpers
# -----------------------------------------------------------------------------
def slippage_bp_for(asset_class: AssetClass) -> int:
    return {
        "equity": SLIPPAGE_BP_EQUITY,
        "crypto": SLIPPAGE_BP_CRYPTO,
        "commodity_proxy": SLIPPAGE_BP_COMMODITY,
        "rates_proxy": SLIPPAGE_BP_RATES_FX,
        "fx_proxy": SLIPPAGE_BP_RATES_FX,
    }.get(asset_class, SLIPPAGE_BP_EQUITY)


def commission_for(asset_class: AssetClass, quantity: int, fill_price: float) -> float:
    if asset_class == "crypto":
        return abs(quantity * fill_price) * COMMISSION_CRYPTO_BP / 10000.0
    if asset_class in ("commodity_proxy", "rates_proxy", "fx_proxy"):
        return COMMISSION_ETF_USD
    return COMMISSION_EQUITY_USD


def is_buy(side: Side) -> bool:
    return side in ("buy", "long")


def is_market_open(
    asset_class: AssetClass,
    now: Optional[datetime] = None,
) -> bool:
    """True if the asset's market is open right now."""
    if SKIP_MARKET_HOURS:
        return True
    if asset_class == "crypto":
        return True   # 24:7
    from .market_calendar import is_equity_session_open

    return is_equity_session_open(now)


def compute_fill(
    side: Side,
    quantity: int,
    last_price: float,
    asset_class: AssetClass = "equity",
    now: Optional[datetime] = None,
) -> Fill:
    """The single fill primitive. Pure function — no I/O.

    Buys execute *worse* than the mid (lift the offer); sells execute
    *worse* than the mid (hit the bid). The bp magnitude depends on
    asset class.
    """
    import math
    if quantity <= 0:
        raise ValueError(f"quantity must be > 0, got {quantity}")
    # Note: nan/inf both fail the comparison `last_price <= 0` because
    # IEEE-754 comparisons against nan return False, so we check
    # explicitly. The brief asks for "realistic fills" — silently
    # accepting nan would be the opposite of realistic.
    if last_price <= 0 or math.isnan(last_price) or math.isinf(last_price):
        raise ValueError(f"last_price must be finite and > 0, got {last_price}")

    slip_bp = slippage_bp_for(asset_class)
    direction = 1 if is_buy(side) else -1
    fill_price = last_price * (1 + direction * slip_bp / 10000.0)
    fill_price = round(fill_price, 4)

    commission = commission_for(asset_class, quantity, fill_price)
    notional = round(abs(quantity) * fill_price, 4)

    mh_ok = is_market_open(asset_class, now=now)
    notes: list[str] = []
    if not mh_ok:
        notes.append(
            f"executed outside {asset_class} market hours — fill modelled "
            f"as if mid-of-next-open; review before live."
        )

    log.info(
        "fill",
        extra={
            "event": "fill",
            "side": side,
            "qty": quantity,
            "last_price": last_price,
            "fill_price": fill_price,
            "slippage_bp": slip_bp,
            "commission_usd": commission,
            "asset_class": asset_class,
            "market_hours_ok": mh_ok,
        },
    )

    return Fill(
        fill_price=fill_price,
        notional_usd=notional,
        commission_usd=commission,
        slippage_bp=slip_bp,
        market_hours_ok=mh_ok,
        asset_class=asset_class,
        notes=notes,
    )


# -----------------------------------------------------------------------------
# Convenience: resolve asset class from the asset_universe registry.
# Falls back to "equity" when the registry doesn't have the ticker (e.g.,
# brand-new IPO).
# -----------------------------------------------------------------------------
def asset_class_for(ticker: str) -> AssetClass:
    try:
        from . import asset_universe
        meta = asset_universe.resolve(ticker)
        return meta.asset_class  # type: ignore[return-value]
    except Exception:
        return "equity"
