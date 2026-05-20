"""yfinance extras aligned with yfinance-mcp tool surface."""
from __future__ import annotations

import time
from typing import Optional

from ... import market_providers


def fetch_price_history(
    ticker: str,
    *,
    period: str = "1mo",
    interval: str = "1d",
) -> dict:
    """OHLCV history — used by agents for drift / chart context."""
    try:
        import yfinance as yf
    except ImportError as e:
        return {"ticker": ticker, "bars": [], "error": "yfinance not installed", "_mcp_provider": "yfinance"}

    sym = market_providers.normalize_yahoo_symbol(ticker)
    try:
        hist = yf.Ticker(sym).history(period=period, interval=interval)
    except Exception as e:
        return {"ticker": ticker, "bars": [], "error": str(e)[:200], "_mcp_provider": "yfinance"}

    if hist is None or hist.empty:
        return {"ticker": ticker, "bars": [], "_mcp_provider": "yfinance"}

    bars = []
    for idx, row in hist.tail(60).iterrows():
        bars.append({
            "date": str(idx.date()) if hasattr(idx, "date") else str(idx)[:10],
            "open": float(row.get("Open") or 0),
            "high": float(row.get("High") or 0),
            "low": float(row.get("Low") or 0),
            "close": float(row.get("Close") or 0),
            "volume": int(row.get("Volume") or 0),
        })
    return {
        "ticker": ticker,
        "period": period,
        "interval": interval,
        "bars": bars,
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "_mcp_provider": "yfinance",
        "_source": "yfinance_history",
    }
