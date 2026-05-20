"""Asset-class registry: equities, crypto (CoinGecko), commodity proxies (ETFs).

Candidate metadata lives in data/candidates.json (`asset_class`, `data_provider`,
`coingecko_id`, etc.). Agents and market_data route quotes/fundamentals by class.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from . import config


@dataclass(frozen=True)
class AssetMeta:
    ticker: str
    asset_class: str  # equity | crypto | commodity_proxy | rates_proxy | fx_proxy
    sector: str
    data_provider: str  # yfinance | coingecko | yfinance_etf
    coingecko_id: Optional[str] = None
    yahoo_symbol: Optional[str] = None
    underlying: Optional[str] = None
    min_market_cap_usd: float = 5_000_000_000
    max_position_pct_nav: float = 0.08
    blurb: str = ""

    @property
    def is_crypto(self) -> bool:
        return self.asset_class == "crypto"

    @property
    def is_commodity_proxy(self) -> bool:
        return self.asset_class == "commodity_proxy"

    @property
    def is_equity(self) -> bool:
        return self.asset_class in ("equity", "")

    @property
    def is_rates_proxy(self) -> bool:
        return self.asset_class == "rates_proxy"

    @property
    def is_fx_proxy(self) -> bool:
        return self.asset_class == "fx_proxy"

    @property
    def is_satellite(self) -> bool:
        return self.asset_class in (
            "crypto", "commodity_proxy", "rates_proxy", "fx_proxy",
        )


_BUILTIN: dict[str, dict[str, Any]] = {
    "BTC": {
        "asset_class": "crypto",
        "sector": "Digital Assets",
        "data_provider": "coingecko",
        "coingecko_id": "bitcoin",
        "min_market_cap_usd": 0,
        "max_position_pct_nav": 0.05,
        "blurb": "Bitcoin — digital store-of-value sleeve; priced via CoinGecko.",
    },
    "ETH": {
        "asset_class": "crypto",
        "sector": "Digital Assets",
        "data_provider": "coingecko",
        "coingecko_id": "ethereum",
        "min_market_cap_usd": 0,
        "max_position_pct_nav": 0.05,
        "blurb": "Ethereum — smart-contract platform; priced via CoinGecko.",
    },
    "GLD": {
        "asset_class": "commodity_proxy",
        "sector": "Commodities",
        "data_provider": "yfinance_etf",
        "underlying": "gold",
        "yahoo_symbol": "GLD",
        "blurb": "SPDR Gold Shares ETF — gold exposure via listed vehicle.",
    },
    "IAU": {
        "asset_class": "commodity_proxy",
        "sector": "Commodities",
        "data_provider": "yfinance_etf",
        "underlying": "gold",
        "yahoo_symbol": "IAU",
        "blurb": "iShares Gold Trust — lower-fee gold proxy.",
    },
    "USO": {
        "asset_class": "commodity_proxy",
        "sector": "Commodities",
        "data_provider": "yfinance_etf",
        "underlying": "crude_oil",
        "yahoo_symbol": "USO",
        "blurb": "United States Oil Fund — WTI crude proxy (contango-aware).",
    },
    "SLV": {
        "asset_class": "commodity_proxy",
        "sector": "Commodities",
        "data_provider": "yfinance_etf",
        "underlying": "silver",
        "yahoo_symbol": "SLV",
        "blurb": "iShares Silver Trust — silver exposure ETF.",
    },
    "DBC": {
        "asset_class": "commodity_proxy",
        "sector": "Commodities",
        "data_provider": "yfinance_etf",
        "underlying": "broad_commodities",
        "yahoo_symbol": "DBC",
        "blurb": "Invesco DB Commodity Index — diversified commodity basket ETF.",
    },
    "TLT": {
        "asset_class": "rates_proxy",
        "sector": "Rates",
        "data_provider": "yfinance_etf",
        "underlying": "us_treasuries_long",
        "yahoo_symbol": "TLT",
        "blurb": "iShares 20+ Year Treasury — duration / rates sleeve.",
    },
    "SHY": {
        "asset_class": "rates_proxy",
        "sector": "Rates",
        "data_provider": "yfinance_etf",
        "underlying": "us_treasuries_short",
        "yahoo_symbol": "SHY",
        "blurb": "iShares 1-3 Year Treasury — short-duration rates.",
    },
    "UUP": {
        "asset_class": "fx_proxy",
        "sector": "Currencies",
        "data_provider": "yfinance_etf",
        "underlying": "usd_index",
        "yahoo_symbol": "UUP",
        "blurb": "Invesco DB US Dollar Index — USD strength proxy.",
    },
    "FXE": {
        "asset_class": "fx_proxy",
        "sector": "Currencies",
        "data_provider": "yfinance_etf",
        "underlying": "euro",
        "yahoo_symbol": "FXE",
        "blurb": "Invesco CurrencyShares Euro — EUR exposure ETF.",
    },
    "IBIT": {
        "asset_class": "commodity_proxy",
        "sector": "Digital Assets",
        "data_provider": "yfinance_etf",
        "underlying": "bitcoin",
        "yahoo_symbol": "IBIT",
        "max_position_pct_nav": 0.05,
        "blurb": "iShares Bitcoin Trust — listed BTC proxy (equity-like).",
    },
}


@lru_cache(maxsize=1)
def _pool_index() -> dict[str, dict[str, Any]]:
    path = config.DATA / "candidates.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in data.get("candidate_pool") or []:
        t = (row.get("ticker") or "").upper()
        if t:
            out[t] = row
    return out


def resolve(ticker: str) -> AssetMeta:
    """Resolve ticker → asset metadata (pool row overrides builtins)."""
    t = (ticker or "").upper().strip()
    row = {**_BUILTIN.get(t, {}), **_pool_index().get(t, {})}
    asset_class = row.get("asset_class") or "equity"
    provider = row.get("data_provider") or (
        "coingecko" if asset_class == "crypto" else "yfinance"
    )
    return AssetMeta(
        ticker=t,
        asset_class=asset_class,
        sector=row.get("sector") or (
            "Digital Assets" if asset_class == "crypto"
            else "Commodities" if asset_class == "commodity_proxy"
            else "Unknown"
        ),
        data_provider=provider,
        coingecko_id=row.get("coingecko_id"),
        yahoo_symbol=row.get("yahoo_symbol") or row.get("ticker"),
        underlying=row.get("underlying"),
        min_market_cap_usd=float(row.get("min_market_cap_usd", 5e9 if asset_class == "equity" else 0)),
        max_position_pct_nav=float(
            row.get("max_position_pct_nav", 0.05 if asset_class == "crypto" else 0.08)
        ),
        blurb=row.get("blurb") or "",
    )


def list_by_asset_class(asset_class: str) -> list[AssetMeta]:
    seen: set[str] = set()
    out: list[AssetMeta] = []
    for t in list(_BUILTIN.keys()) + list(_pool_index().keys()):
        if t in seen:
            continue
        seen.add(t)
        meta = resolve(t)
        if meta.asset_class == asset_class:
            out.append(meta)
    return out


def pool_tickers() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for t in list(_BUILTIN.keys()) + list(_pool_index().keys()):
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def pool_rows() -> dict[str, dict[str, Any]]:
    """Merged builtin + candidates.json metadata per ticker."""
    out: dict[str, dict[str, Any]] = {}
    for t in pool_tickers():
        m = resolve(t)
        out[t] = {
            "ticker": t,
            "sector": m.sector,
            "asset_class": m.asset_class,
            "blurb": m.blurb,
        }
    return out


def format_for_prompt() -> str:
    """Short summary for agent system prompts."""
    crypto = [m.ticker for m in list_by_asset_class("crypto")]
    cmdty = [m.ticker for m in list_by_asset_class("commodity_proxy")]
    rates = [m.ticker for m in list_by_asset_class("rates_proxy")]
    fx = [m.ticker for m in list_by_asset_class("fx_proxy")]
    return (
        "Asset data routing: US equities → yfinance + SEC EDGAR; "
        f"crypto ({', '.join(crypto) or 'none'}) → CoinGecko; "
        f"commodities ({', '.join(cmdty) or 'ETFs'}) → yfinance; "
        f"rates ({', '.join(rates) or 'TLT/SHY'}) → yfinance; "
        f"FX ({', '.join(fx) or 'UUP/FXE'}) → yfinance + Frankfurter context. "
        "See policy §8 multi-asset data."
    )
