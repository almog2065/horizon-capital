"""Market-data MCP bridge — free providers, native adapters (no API keys).

Maps MCP-equivalent tools to in-process calls (yfinance, SEC EDGAR,
CoinGecko public API, Frankfurter). Optional Cursor MCP servers are
documented in docs/mcp-market-data.md and .cursor/mcp.json.example.
"""
from .bridge import (
    discover_idea_candidates,
    discover_recent_8k,
    fetch_fundamentals,
    fetch_macro_context,
    fetch_news_for_ticker,
    fetch_quote,
    fetch_recent_filings_for_ticker,
    list_providers,
    provider_status,
)

__all__ = [
    "fetch_quote",
    "fetch_fundamentals",
    "fetch_news_for_ticker",
    "fetch_recent_filings_for_ticker",
    "discover_recent_8k",
    "discover_idea_candidates",
    "fetch_macro_context",
    "list_providers",
    "provider_status",
]
