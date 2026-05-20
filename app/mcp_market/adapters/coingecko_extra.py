"""CoinGecko public REST (keyless) — complements native coingecko quotes."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from ...market_data import MarketDataError, _http_get, _mark_health

COINGECKO_API = "https://api.coingecko.com/api/v3"
TRENDING_URL = f"{COINGECKO_API}/search/trending"
GLOBAL_URL = f"{COINGECKO_API}/global"


def fetch_trending(top_n: int = 10) -> dict:
    """Top trending coins (idea-scan supplemental channel)."""
    try:
        raw = _http_get(TRENDING_URL, timeout=10.0)
        data = json.loads(raw)
        coins = []
        for row in (data.get("coins") or [])[:top_n]:
            item = row.get("item") or {}
            sym = (item.get("symbol") or "").upper()
            if sym:
                coins.append({
                    "ticker": sym,
                    "name": item.get("name", ""),
                    "market_cap_rank": item.get("market_cap_rank"),
                    "score": float(row.get("score") or 0),
                })
        _mark_health("coingecko", True)
        return {
            "source": "coingecko_trending",
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "coins": coins,
            "_mcp_provider": "coingecko",
        }
    except Exception as e:
        _mark_health("coingecko", False, str(e))
        return {"source": "coingecko_trending", "coins": [], "error": str(e)[:200]}


def fetch_global_market() -> dict:
    try:
        raw = _http_get(GLOBAL_URL, timeout=10.0)
        data = json.loads(raw)
        g = data.get("data") or {}
        return {
            "source": "coingecko_global",
            "total_market_cap_usd": float((g.get("total_market_cap") or {}).get("usd") or 0),
            "btc_dominance_pct": float(g.get("market_cap_percentage", {}).get("btc") or 0),
            "_mcp_provider": "coingecko",
        }
    except Exception as e:
        raise MarketDataError("coingecko", str(e)[:200]) from e
