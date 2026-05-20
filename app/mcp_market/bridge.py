"""Route agent market-data tools through free MCP-equivalent providers."""
from __future__ import annotations

from typing import Any, Optional

from ..core.settings import get_settings
from .. import market_data
from . import registry
from .adapters import coingecko_extra, yfinance_extra


def _tag(result: dict, provider_id: str, tool: str) -> dict:
    if isinstance(result, dict):
        result.setdefault("_mcp_provider", provider_id)
        result.setdefault("_mcp_tool", tool)
    return result


def list_providers() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "tools": list(p.tools),
            "horizon_native": p.horizon_native,
            "mcp_package": p.mcp_package,
            "requires_key": p.requires_key,
            "notes": p.notes,
        }
        for p in registry.FREE_MARKET_PROVIDERS
    ]


def provider_status() -> dict[str, Any]:
    """Health from market_data + MCP extras."""
    base = market_data.health_status()
    cfg = get_settings()
    out = {
        "enabled": cfg.MCP_MARKET_ENABLED,
        "coingecko_trending": cfg.MCP_COINGECKO_TRENDING,
        "yfinance_history": cfg.MCP_YFINANCE_HISTORY,
        "providers": list_providers(),
        "sources": base,
    }
    if cfg.MCP_COINGECKO_TRENDING:
        try:
            tr = coingecko_extra.fetch_trending(3)
            out["coingecko_trending_ok"] = not tr.get("error")
        except Exception as e:
            out["coingecko_trending_ok"] = False
            out["coingecko_trending_error"] = str(e)[:120]
    return out


def fetch_quote(ticker: str) -> dict:
    from .. import asset_universe
    meta = asset_universe.resolve(ticker)
    q = market_data.fetch_quote_real(ticker)
    pid = "coingecko" if meta.is_crypto else "yfinance"
    return _tag(q, pid, "fetch_quote")


def fetch_fundamentals(ticker: str) -> dict:
    f = market_data.fetch_fundamentals_real(ticker)
    from .. import asset_universe
    pid = "coingecko" if asset_universe.resolve(ticker).is_crypto else "yfinance"
    return _tag(f, pid, "fetch_fundamentals")


def fetch_news_for_ticker(ticker: str, top_k: int = 5) -> dict:
    return _tag(
        market_data.fetch_news_for_ticker_real(ticker, top_k=top_k),
        "yfinance",
        "fetch_news",
    )


def fetch_recent_filings_for_ticker(ticker: str, top_k: int = 5) -> dict:
    return _tag(
        market_data.fetch_recent_filings_for_ticker(ticker, top_k=top_k),
        "sec_edgar",
        "fetch_filings",
    )


def discover_recent_8k(count: int = 40, exclude: Optional[set] = None) -> dict:
    return _tag(
        market_data.discover_recent_8k_tickers(count=count, exclude=exclude or set()),
        "sec_edgar",
        "discover_8k",
    )


def discover_idea_candidates(
    count_per_form: int = 25,
    exclude: Optional[set] = None,
) -> dict:
    excl = exclude or set()
    base = market_data.discover_idea_candidates(count_per_form=count_per_form, exclude=excl)
    cfg = get_settings()
    if cfg.MCP_MARKET_ENABLED and cfg.MCP_COINGECKO_TRENDING and cfg.ENABLE_COINGECKO:
        trending = coingecko_extra.fetch_trending(8)
        extra_syms = [
            c["ticker"] for c in trending.get("coins", [])
            if c.get("ticker") and c["ticker"] not in excl
        ]
        merged = list(dict.fromkeys((base.get("tickers") or []) + extra_syms))
        base["tickers"] = merged
        base["coingecko_trending"] = trending.get("coins", [])
        base["_mcp_supplement"] = "coingecko_trending"
    return _tag(base, "sec_edgar", "discover_idea_candidates")


def fetch_macro_context() -> dict:
    ctx = market_data.fetch_macro_context()
    cfg = get_settings()
    if cfg.MCP_MARKET_ENABLED and cfg.MCP_COINGECKO_TRENDING:
        try:
            g = coingecko_extra.fetch_global_market()
            ctx["coingecko_global"] = g
        except Exception:
            pass
    ctx["_mcp_providers"] = ["frankfurter", "coingecko", "yfinance"]
    return ctx


def fetch_price_history(ticker: str, period: str = "1mo") -> dict:
    cfg = get_settings()
    if not cfg.MCP_MARKET_ENABLED or not cfg.MCP_YFINANCE_HISTORY:
        return {"ticker": ticker, "bars": [], "disabled": True}
    return yfinance_extra.fetch_price_history(ticker, period=period)
