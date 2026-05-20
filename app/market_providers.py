"""Free fallback market-data providers when yfinance fails.

No API keys required for: Yahoo chart JSON, Stooq CSV, SEC EDGAR company
facts/submissions, Google News RSS.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Optional
from xml.etree import ElementTree as ET

from .market_data import EDGAR_UA, MarketDataError, _http_get

YAHOO_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?interval=1d&range=5d"
)
STOOQ_CSV_URL = "https://stooq.com/q/l/?s={symbol}.us&f=sd2t2ohlcv&h&e=csv"
EDGAR_DATA = "https://data.sec.gov"
EDGAR_COMPANY_FACTS = f"{EDGAR_DATA}/api/xbrl/companyfacts/CIK{{cik:010d}}.json"
GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q={query}&hl=en-US&gl=US&ceid=US:en"
)

COINGECKO_API = "https://api.coingecko.com/api/v3"
COINGECKO_SIMPLE_PRICE = (
    f"{COINGECKO_API}/simple/price"
    "?ids={ids}&vs_currencies=usd"
    "&include_24hr_change=true&include_market_cap=true"
    "&include_last_updated_at=true"
)
COINGECKO_COIN = f"{COINGECKO_API}/coins/" + "{coin_id}"

# Preferred US-GAAP tags for fundamentals (first hit wins).
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
_OP_INCOME_TAGS = ("OperatingIncomeLoss",)
_NET_INCOME_TAGS = ("NetIncomeLoss",)


def normalize_yahoo_symbol(ticker: str) -> str:
    """Map firm tickers to Yahoo symbols (BRK.B → BRK-B)."""
    t = (ticker or "").upper().strip()
    if not t:
        return t
    # Class shares and similar: dots → hyphens for Yahoo.
    if "." in t and "-" not in t:
        t = t.replace(".", "-")
    return t


def stooq_symbol(ticker: str) -> str:
    return normalize_yahoo_symbol(ticker).lower()


def parse_yfinance_news_item(item: dict) -> dict:
    """Normalize yfinance ≥1.0 news shape (nested `content`) to flat hit."""
    content = item.get("content") if isinstance(item.get("content"), dict) else item
    title = (content.get("title") or item.get("title") or "").strip()
    summary = (content.get("summary") or content.get("description")
               or item.get("summary") or "")[:400]
    pub = content.get("pubDate") or content.get("displayTime") or ""
    provider = content.get("provider") or {}
    publisher = ""
    if isinstance(provider, dict):
        publisher = provider.get("displayName") or provider.get("name") or ""
    publisher = publisher or item.get("publisher") or "Yahoo Finance"
    link = (
        content.get("canonicalUrl")
        or content.get("clickThroughUrl")
        or item.get("link")
        or ""
    )
    if isinstance(link, dict):
        link = link.get("url", "")
    ts = item.get("providerPublishTime")
    published_at = ""
    if pub:
        published_at = pub.replace("Z", "").replace("T", " ")[:19]
    elif ts:
        published_at = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(int(ts)),
        )
    return {
        "title": title,
        "url": str(link) if link else "",
        "publisher": publisher,
        "published_at": published_at,
        "summary": summary,
    }


def fetch_quote_yahoo_chart(symbol: str, ticker: str) -> dict:
    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        raw = _http_get(url, headers={"User-Agent": YAHOO_UA}, timeout=8.0)
    except urllib.error.HTTPError as e:
        raise MarketDataError(
            "yahoo_chart", f"HTTP {e.code}: {e.reason}", ticker,
        ) from e
    except Exception as e:
        raise MarketDataError("yahoo_chart", str(e)[:200], ticker) from e
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise MarketDataError("yahoo_chart", "empty chart result", ticker)
    meta = result[0].get("meta") or {}
    price = float(meta.get("regularMarketPrice") or 0)
    if not price:
        quotes = (result[0].get("indicators") or {}).get("quote") or []
        closes = (quotes[0].get("close") if quotes else None) or []
        closes = [c for c in closes if c is not None]
        if not closes:
            raise MarketDataError("yahoo_chart", "no price in chart", ticker)
        price = float(closes[-1])
    vol = int(meta.get("regularMarketVolume") or 0)
    return {
        "ticker": ticker,
        "price": price,
        "bid": price - 0.05,
        "ask": price + 0.05,
        "volume_today": vol,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source": "yahoo_chart",
        "_yahoo_symbol": symbol,
    }


def fetch_quote_stooq(ticker: str) -> dict:
    sym = stooq_symbol(ticker)
    url = STOOQ_CSV_URL.format(symbol=sym)
    try:
        raw = _http_get(url, headers={"User-Agent": YAHOO_UA}, timeout=8.0).decode()
    except Exception as e:
        raise MarketDataError("stooq", str(e)[:200], ticker) from e
    rows = list(csv.DictReader(io.StringIO(raw)))
    if not rows:
        raise MarketDataError("stooq", "empty CSV", ticker)
    row = rows[-1]
    price = float(row.get("Close") or 0)
    if not price:
        raise MarketDataError("stooq", "no close price", ticker)
    vol = int(float(row.get("Volume") or 0))
    return {
        "ticker": ticker,
        "price": price,
        "bid": price - 0.05,
        "ask": price + 0.05,
        "volume_today": vol,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source": "stooq",
    }


def _edgar_latest_fy_values(fact_block: dict, n: int = 2) -> list[dict]:
    units = (fact_block.get("units") or {}).get("USD") or []
    fy = [u for u in units if u.get("form") == "10-K" and u.get("fp") == "FY"]
    fy.sort(key=lambda u: u.get("end", ""))
    return fy[-n:]


def _pick_fact(usgaap: dict, tags: tuple[str, ...]) -> Optional[dict]:
    for tag in tags:
        if tag in usgaap:
            return usgaap[tag]
    return None


def fetch_fundamentals_edgar(
    ticker: str,
    cik: int,
    quote_price: Optional[float] = None,
    shares_outstanding: Optional[float] = None,
) -> dict:
    url = EDGAR_COMPANY_FACTS.format(cik=cik)
    raw = _http_get(url, headers={"User-Agent": EDGAR_UA}, timeout=12.0)
    data = json.loads(raw)
    entity = data.get("entityName", ticker)
    usgaap = (data.get("facts") or {}).get("us-gaap") or {}

    rev_block = _pick_fact(usgaap, _REVENUE_TAGS)
    op_block = _pick_fact(usgaap, _OP_INCOME_TAGS)
    if not rev_block:
        raise MarketDataError(
            "edgar_facts", "no revenue facts in companyfacts", ticker,
        )

    rev_fy = _edgar_latest_fy_values(rev_block, 2)
    latest_rev = float(rev_fy[-1]["val"])
    rev_growth = 0.0
    if len(rev_fy) >= 2 and rev_fy[-2]["val"]:
        prev = float(rev_fy[-2]["val"])
        rev_growth = (latest_rev - prev) / prev if prev else 0.0

    op_margin = 0.0
    if op_block:
        op_fy = _edgar_latest_fy_values(op_block, 1)
        if op_fy and latest_rev:
            op_margin = float(op_fy[-1]["val"]) / latest_rev

    mcap = 0.0
    pe = 0.0
    if quote_price and shares_outstanding:
        mcap = quote_price * shares_outstanding
    elif quote_price and mcap == 0:
        sh_block = usgaap.get("CommonStockSharesOutstanding") or usgaap.get(
            "EntityCommonStockSharesOutstanding"
        )
        if sh_block:
            sh_vals = (sh_block.get("units") or {}).get("shares") or []
            if sh_vals:
                shares_outstanding = float(
                    sorted(sh_vals, key=lambda u: u.get("end", ""))[-1]["val"]
                )
                mcap = quote_price * shares_outstanding

    return {
        "ticker": ticker,
        "pe_ttm": float(pe or 0),
        "ev_ebitda": 0.0,
        "fcf_yield_pct": 0.0,
        "revenue_growth_yoy": float(rev_growth),
        "operating_margin": float(op_margin),
        "next_earnings_in_days": None,
        "market_cap_usd": float(mcap or 0),
        "_source": "edgar_companyfacts",
        "_name": entity,
        "_sector": "",
        "_industry": "",
        "_business_summary": (
            f"{entity} — fundamentals from SEC XBRL company facts "
            f"(FY revenue growth {rev_growth:.1%}, op margin {op_margin:.1%})."
        ),
        "_edgar_cik": cik,
    }


def fetch_quote_coingecko(coin_id: str, ticker: str) -> dict:
    """Spot USD quote via CoinGecko public API (no key; ~10–30 req/min)."""
    url = COINGECKO_SIMPLE_PRICE.format(ids=urllib.parse.quote(coin_id))
    try:
        raw = _http_get(url, headers={"User-Agent": YAHOO_UA}, timeout=10.0)
        data = json.loads(raw)
    except Exception as e:
        raise MarketDataError("coingecko", f"request failed: {e}", ticker) from e
    row = data.get(coin_id) or {}
    price = row.get("usd")
    if price is None:
        raise MarketDataError(
            "coingecko", f"no USD price for coin_id={coin_id}", ticker,
        )
    price = float(price)
    updated = row.get("last_updated_at")
    ts = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(updated)))
        if updated else time.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {
        "ticker": ticker,
        "price": price,
        "bid": price * 0.9995,
        "ask": price * 1.0005,
        "volume_today": 0,
        "timestamp": ts,
        "price_change_24h_pct": float(row.get("usd_24h_change") or 0),
        "market_cap_usd": float(row.get("usd_market_cap") or 0),
        "_source": "coingecko",
        "_coingecko_id": coin_id,
    }


def fetch_fundamentals_coingecko(coin_id: str, ticker: str) -> dict:
    """Crypto fundamentals proxy: market cap + momentum from CoinGecko."""
    quote = fetch_quote_coingecko(coin_id, ticker)
    mcap = float(quote.get("market_cap_usd") or 0)
    ch24 = float(quote.get("price_change_24h_pct") or 0) / 100.0
    # Map 24h return into a synthetic growth field for scoring heuristics
    return {
        "ticker": ticker,
        "pe_ttm": 0.0,
        "ev_ebitda": 0.0,
        "fcf_yield_pct": 0.0,
        "revenue_growth_yoy": ch24,
        "operating_margin": 0.0,
        "next_earnings_in_days": None,
        "market_cap_usd": mcap,
        "price_change_24h_pct": ch24 * 100,
        "_source": "coingecko",
        "_asset_class": "crypto",
        "_coingecko_id": coin_id,
        "_name": ticker,
        "_sector": "Digital Assets",
        "_industry": "Cryptocurrency",
        "_business_summary": (
            f"{ticker} spot crypto asset; fundamentals are network/market "
            "metrics, not GAAP financials."
        ),
    }


def fetch_news_google_rss(ticker: str, top_k: int = 5) -> list[dict]:
    query = urllib.parse.quote(f"{ticker} stock")
    url = GOOGLE_NEWS_RSS.format(query=query)
    raw = _http_get(url, headers={"User-Agent": YAHOO_UA}, timeout=8.0)
    root = ET.fromstring(raw)
    hits: list[dict] = []
    for item in root.findall(".//item")[:top_k]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "")[:25]
        desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "")[:400]
        if not title:
            continue
        hits.append({
            "title": title,
            "url": link,
            "publisher": "Google News",
            "published_at": pub,
            "summary": desc,
        })
    return hits


def fetch_news_google_rss_crypto(ticker: str, coin_name: str, top_k: int = 5) -> list[dict]:
    """Google News RSS for crypto (no yfinance ticker news)."""
    query = urllib.parse.quote(f"{coin_name} cryptocurrency {ticker}")
    url = GOOGLE_NEWS_RSS.format(query=query)
    raw = _http_get(url, headers={"User-Agent": YAHOO_UA}, timeout=8.0)
    root = ET.fromstring(raw)
    hits: list[dict] = []
    for item in root.findall(".//item")[:top_k]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "")[:25]
        desc = re.sub(r"<[^>]+>", "", item.findtext("description") or "")[:400]
        if not title:
            continue
        hits.append({
            "title": title,
            "url": link,
            "publisher": "Google News",
            "published_at": pub,
            "summary": desc,
        })
    return hits


FRANKFURTER_LATEST = "https://api.frankfurter.app/latest?from=USD&to=EUR,GBP,JPY,CHF,ILS"


def fetch_fx_rates_usd() -> dict:
    """Spot FX vs USD from Frankfurter (ECB data, no API key)."""
    try:
        raw = _http_get(FRANKFURTER_LATEST, headers={"User-Agent": YAHOO_UA}, timeout=8.0)
        data = json.loads(raw)
    except Exception as e:
        raise MarketDataError("frankfurter", str(e)[:200]) from e
    rates = data.get("rates") or {}
    if not rates:
        raise MarketDataError("frankfurter", "empty rates")
    return {
        "base": data.get("base", "USD"),
        "date": data.get("date", ""),
        "rates": {k: float(v) for k, v in rates.items()},
        "_source": "frankfurter",
    }
