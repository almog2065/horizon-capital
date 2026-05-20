"""Catalog of free market-data sources (MCP-compatible, no paid keys)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderKind = Literal["native", "cursor_mcp_only"]


@dataclass(frozen=True)
class MarketProvider:
    id: str
    name: str
    kind: ProviderKind
    mcp_package: str  # npm/pypi name or URL for Cursor config
    tools: tuple[str, ...]
    notes: str
    requires_key: bool = False
    horizon_native: bool = True  # wired in app/mcp_market/bridge.py


# Free MCP servers researched (2026) — horizon_native=True means app uses same data in-process.
FREE_MARKET_PROVIDERS: tuple[MarketProvider, ...] = (
    MarketProvider(
        id="yfinance",
        name="Yahoo Finance (yfinance)",
        kind="native",
        mcp_package="uvx yfinance-mcp / narumiruna/yfinance-mcp",
        tools=(
            "fetch_quote", "fetch_fundamentals", "fetch_news_for_ticker",
            "fetch_price_history", "fetch_company_profile",
        ),
        notes="US equities & ETFs; primary quote path.",
        horizon_native=True,
    ),
    MarketProvider(
        id="sec_edgar",
        name="SEC EDGAR",
        kind="native",
        mcp_package="npx mcp-edgar / stefanoamorelli/sec-edgar-mcp",
        tools=(
            "discover_recent_8k", "discover_idea_candidates",
            "fetch_recent_filings_for_ticker", "fetch_fundamentals_edgar",
        ),
        notes="Filings discovery & XBRL fundamentals; User-Agent required.",
        horizon_native=True,
    ),
    MarketProvider(
        id="coingecko",
        name="CoinGecko Public API",
        kind="native",
        mcp_package="npx mcp-remote https://mcp.api.coingecko.com/mcp",
        tools=("fetch_quote_crypto", "fetch_trending_crypto", "fetch_global_crypto"),
        notes="BTC/ETH spot; optional MCP remote for Cursor dev.",
        horizon_native=True,
    ),
    MarketProvider(
        id="frankfurter",
        name="Frankfurter FX (ECB)",
        kind="native",
        mcp_package="—",
        tools=("fetch_fx_rates_usd",),
        notes="USD FX context for macro prompts.",
        horizon_native=True,
    ),
    MarketProvider(
        id="yahoo_chart",
        name="Yahoo Chart JSON",
        kind="native",
        mcp_package="—",
        tools=("fetch_quote_fallback",),
        notes="Fallback when yfinance fails.",
        horizon_native=True,
    ),
    MarketProvider(
        id="stooq",
        name="Stooq CSV",
        kind="native",
        mcp_package="—",
        tools=("fetch_quote_fallback",),
        notes="Second fallback for US symbols.",
        horizon_native=True,
    ),
    MarketProvider(
        id="google_news_rss",
        name="Google News RSS",
        kind="native",
        mcp_package="—",
        tools=("fetch_news_fallback",),
        notes="News when yfinance news empty.",
        horizon_native=True,
    ),
)


def list_native_providers() -> list[MarketProvider]:
    return [p for p in FREE_MARKET_PROVIDERS if p.horizon_native]


def get_provider(provider_id: str) -> MarketProvider | None:
    return next((p for p in FREE_MARKET_PROVIDERS if p.id == provider_id), None)
