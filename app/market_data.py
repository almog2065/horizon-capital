"""External market-data layer.

Real-data sources:
  - SEC EDGAR (free, no key) — recent 8-K filings → discovery of tickers
    that just had a material corporate event. Used for proactive idea
    generation: any ticker that just filed a material event is a candidate
    for research.
  - yfinance (free, no key) — US equities & commodity ETFs (primary).
  - CoinGecko (free, no key) — spot crypto (BTC, ETH, …).
  - Fallbacks (no key): Yahoo chart JSON, Stooq CSV, SEC EDGAR company facts,
    Google News RSS.

ERROR POLICY (changed): when a real source fails (no network, blocked,
yfinance missing, ticker not found), the call **raises `MarketDataError`**
rather than silently returning hash-based mock data. The caller is
responsible for surfacing the error so downstream agents (and the LLM)
know the data is missing and can refuse / escalate rather than reason
over invented numbers.

The `_offline_*` helpers below are kept as a last-resort utility for
deliberately-offline tests, but they are NEVER returned automatically
from the public `fetch_*_real` functions.

Caching: in-process TTL cache keyed by call signature so a single scan
doesn't hammer the same endpoint repeatedly.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from typing import Optional
from xml.etree import ElementTree as ET


class MarketDataError(Exception):
    """Raised when a real market-data call fails.

    Carries the source name and original error so the caller can surface
    a precise reason instead of silently substituting fake data.
    """
    def __init__(self, source: str, message: str,
                 ticker: Optional[str] = None):
        self.source = source
        self.ticker = ticker
        self.message = message
        super().__init__(f"[{source}] {message}"
                          + (f" (ticker={ticker})" if ticker else ""))

# Required by SEC EDGAR fair-use policy — must identify the caller.
EDGAR_UA = "Horizon Capital Demo (almogbensim@gmail.com)"
EDGAR_BASE = "https://www.sec.gov"
EDGAR_DATA = "https://data.sec.gov"
EDGAR_RECENT_ATOM = (
    f"{EDGAR_BASE}/cgi-bin/browse-edgar"
    "?action=getcurrent&type={form_type}&company=&dateb=&owner=include"
    "&count={count}&output=atom"
)
EDGAR_RECENT_8K = EDGAR_RECENT_ATOM  # backward-compatible alias

# 8-K items treated as higher-signal discovery triggers for the scan layer.
MATERIAL_8K_ITEMS = frozenset({
    "1.01", "1.02", "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
    "5.01", "5.02", "5.03", "7.01", "8.01",
})

MIN_DISCOVERY_MARKET_CAP_USD = 5_000_000_000
EDGAR_TICKERS_MAP = f"{EDGAR_BASE}/files/company_tickers.json"
EDGAR_SUBMISSIONS = f"{EDGAR_DATA}/submissions/CIK{{cik:010d}}.json"

# Module-level cache: { (fn_name, *args): (value, expires_at) }
_CACHE: dict[tuple, tuple] = {}
_DEFAULT_TTL_SECONDS = 600  # 10 minutes — fine for a scan, not for prod
_HEALTH = {
    "edgar": {"ok": None, "last_error": None, "last_check": 0},
    "yfinance": {"ok": None, "last_error": None, "last_check": 0},
    "coingecko": {"ok": None, "last_error": None, "last_check": 0},
    "frankfurter": {"ok": None, "last_error": None, "last_check": 0},
    "yahoo_chart": {"ok": None, "last_error": None, "last_check": 0},
    "stooq": {"ok": None, "last_error": None, "last_check": 0},
    "edgar_facts": {"ok": None, "last_error": None, "last_check": 0},
    "google_news": {"ok": None, "last_error": None, "last_check": 0},
}


def _cik_for_ticker(ticker: str) -> Optional[int]:
    sym = ticker.upper()
    for cik, meta in _edgar_cik_to_ticker_map().items():
        if meta.get("ticker") == sym:
            return cik
    return None


def _mark_health(source: str, ok: bool, error: Optional[str] = None) -> None:
    _HEALTH[source] = {
        "ok": ok,
        "last_error": (error[:200] if error else None),
        "last_check": time.time(),
    }


# ---------- cache helpers ----------

def _cached(key: tuple, ttl: float, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and hit[1] > now:
        return hit[0]
    value = fn()
    _CACHE[key] = (value, now + ttl)
    return value


def _http_get(url: str, headers: Optional[dict] = None,
              timeout: float = 6.0) -> bytes:
    req_headers = {"User-Agent": EDGAR_UA, "Accept-Encoding": "gzip, deflate"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        enc = resp.headers.get("Content-Encoding", "")
        if "gzip" in enc:
            import gzip
            data = gzip.decompress(data)
        elif "deflate" in enc:
            import zlib
            data = zlib.decompress(data)
        return data


# ---------- EDGAR ----------

def _edgar_cik_to_ticker_map() -> dict[int, dict]:
    """Returns {cik_int: {ticker, name, exchange?}}.

    SEC company_tickers.json schema: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    """
    def fetch():
        raw = _http_get(EDGAR_TICKERS_MAP, timeout=6.0)
        data = json.loads(raw)
        out: dict[int, dict] = {}
        for v in data.values():
            cik = int(v.get("cik_str") or 0)
            if cik:
                out[cik] = {
                    "ticker": v.get("ticker", "").upper(),
                    "name": v.get("title", ""),
                }
        return out
    return _cached(("edgar_tickers",), ttl=24 * 3600, fn=fetch)


def _parse_edgar_atom_feed(atom_bytes: bytes, form_type: str,
                           exclude: set[str]) -> list[dict]:
    """Parse EDGAR current-filings Atom feed into filing dicts."""
    root = ET.fromstring(atom_bytes)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    cik_map = _edgar_cik_to_ticker_map()

    filings: list[dict] = []
    for e in entries:
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        link_el = e.find("a:link", ns)
        url = link_el.get("href") if link_el is not None else ""
        updated = e.findtext("a:updated", default="", namespaces=ns) or ""
        summary = (e.findtext("a:summary", default="", namespaces=ns) or "")

        m = re.search(r"\(CIK\s+(\d+)\)", title)
        if not m:
            continue
        cik = int(m.group(1))
        mapping = cik_map.get(cik)
        if not mapping or not mapping.get("ticker"):
            continue
        ticker = mapping["ticker"]
        if ticker in exclude:
            continue

        acc_m = re.search(r"/(\d{10}-\d{2}-\d{6})-index", url)
        accession = acc_m.group(1) if acc_m else ""
        items = re.findall(r"\bItem\s+(\d+\.\d+)", summary)
        material = bool(items) and any(i in MATERIAL_8K_ITEMS for i in items)

        filings.append({
            "ticker": ticker,
            "company": mapping.get("name", ""),
            "cik": cik,
            "form": form_type,
            "filing_url": url,
            "accession": accession,
            "filed_at": updated,
            "items": items,
            "material_event": material if form_type == "8-K" else False,
            "discovery_priority": (
                2 if (form_type == "8-K" and material) else
                1 if form_type == "8-K" else 0
            ),
        })
    return filings


def _fetch_edgar_recent_form(form_type: str, count: int,
                              exclude: set[str]) -> list[dict]:
    url = EDGAR_RECENT_ATOM.format(form_type=form_type, count=count)
    atom = _http_get(url, timeout=8.0)
    return _parse_edgar_atom_feed(atom, form_type, exclude)


def discover_recent_8k_tickers(count: int = 40,
                                exclude: Optional[set] = None) -> dict:
    """Pull the most recent 8-K filings from EDGAR and return tickers + meta."""
    exclude = exclude or set()
    try:
        filings = _fetch_edgar_recent_form("8-K", count, exclude)
        seen: set[str] = set()
        tickers: list[str] = []
        for f in filings:
            if f["ticker"] not in seen:
                seen.add(f["ticker"])
                tickers.append(f["ticker"])

        _HEALTH["edgar"] = {
            "ok": True, "last_error": None, "last_check": time.time(),
        }
        return {
            "source": "edgar",
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "filings": filings,
            "tickers": tickers,
            "error": None,
        }
    except Exception as e:
        _HEALTH["edgar"] = {
            "ok": False, "last_error": str(e)[:200], "last_check": time.time(),
        }
        return {
            "source": "fallback",
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "filings": [], "tickers": [],
            "error": str(e),
        }


def discover_idea_candidates(
    count_per_form: int = 25,
    exclude: Optional[set] = None,
    forms: tuple[str, ...] = ("8-K", "10-Q"),
) -> dict:
    """Multi-channel EDGAR discovery for the Idea Generator scan layer.

    Merges recent 8-K and 10-Q streams, dedupes by ticker (highest priority
    filing wins), and tags material 8-K items for ranking boosts.
    """
    exclude = exclude or set()
    merged: dict[str, dict] = {}
    all_filings: list[dict] = []
    errors: list[str] = []

    for form in forms:
        try:
            batch = _fetch_edgar_recent_form(form, count_per_form, exclude)
            all_filings.extend(batch)
            for f in batch:
                t = f["ticker"]
                prev = merged.get(t)
                if prev is None or f["discovery_priority"] > prev.get(
                    "discovery_priority", 0
                ):
                    merged[t] = f
        except Exception as e:
            errors.append(f"{form}: {str(e)[:120]}")

    if not merged and errors:
        _HEALTH["edgar"] = {
            "ok": False, "last_error": "; ".join(errors)[:200],
            "last_check": time.time(),
        }
        return {
            "source": "fallback",
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "filings": [],
            "tickers": [],
            "by_ticker": {},
            "channels": list(forms),
            "error": "; ".join(errors),
        }

    _HEALTH["edgar"] = {
        "ok": True, "last_error": None, "last_check": time.time(),
    }
    tickers = list(merged.keys())
    return {
        "source": "edgar",
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "filings": all_filings,
        "tickers": tickers,
        "by_ticker": merged,
        "channels": list(forms),
        "material_8k_count": sum(
            1 for f in merged.values() if f.get("material_event")
        ),
        "error": "; ".join(errors) if errors else None,
    }


def fetch_recent_filings_for_ticker(ticker: str, top_k: int = 5) -> dict:
    """All recent filings for a given ticker via /submissions/.

    Returns hits in the same shape as RAG search so the brief builder
    can cite them: {"hits": [{ref, source_type, url, what_it_supports, ...}]}
    """
    try:
        cik_map = _edgar_cik_to_ticker_map()
        cik = None
        for k, v in cik_map.items():
            if v.get("ticker") == ticker.upper():
                cik = k
                break
        if cik is None:
            return {"source": "fallback", "hits": [],
                    "error": f"ticker {ticker} not in EDGAR map"}

        url = EDGAR_SUBMISSIONS.format(cik=cik)

        def fetch():
            raw = _http_get(url, timeout=8.0)
            return json.loads(raw)

        data = _cached(("edgar_subs", cik), ttl=6 * 3600, fn=fetch)
        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        dates = recent.get("filingDate") or []
        primary = recent.get("primaryDocument") or []

        hits = []
        for form, acc, dt, doc in zip(forms, accessions, dates, primary):
            if form not in ("8-K", "10-K", "10-Q", "DEF 14A"):
                continue
            acc_clean = acc.replace("-", "")
            doc_url = (
                f"{EDGAR_BASE}/Archives/edgar/data/{cik}/{acc_clean}/{doc}"
            )
            hits.append({
                "source_type": "filing",
                "form": form,
                "ref": f"{ticker} {form} {acc}",
                "url": doc_url,
                "filed_at": dt,
                "what_it_supports": (
                    "material_event" if form == "8-K"
                    else "fundamentals_disclosure"
                ),
            })
            if len(hits) >= top_k:
                break

        _HEALTH["edgar"] = {
            "ok": True, "last_error": None, "last_check": time.time(),
        }
        return {"source": "edgar", "hits": hits, "error": None}
    except Exception as e:
        _HEALTH["edgar"] = {
            "ok": False, "last_error": str(e)[:200], "last_check": time.time(),
        }
        return {"source": "fallback", "hits": [], "error": str(e)}


# ---------- yfinance ----------

_yf_module = None
_yf_load_attempted = False


def _yf():
    global _yf_module, _yf_load_attempted
    if not _yf_load_attempted:
        _yf_load_attempted = True
        try:
            import yfinance as yf  # noqa
            _yf_module = yf
            _HEALTH["yfinance"]["ok"] = True
        except Exception as e:
            _yf_module = None
            _HEALTH["yfinance"] = {
                "ok": False, "last_error": f"yfinance not installed: {e}",
                "last_check": time.time(),
            }
    return _yf_module


def _offline_fundamentals_stub(ticker: str) -> dict:
    """Deterministic hash-based stub for offline tests ONLY. NOT auto-returned."""
    h = int(hashlib.sha256((ticker + "fund").encode()).hexdigest()[:6], 16)
    return {
        "ticker": ticker,
        "pe_ttm": 12 + (h % 30),
        "ev_ebitda": 8 + (h % 18),
        "fcf_yield_pct": 2 + (h % 8),
        "revenue_growth_yoy": 0.05 + ((h % 25) / 100),
        "operating_margin": 0.10 + ((h % 30) / 100),
        "next_earnings_in_days": (h % 60) + 1,
        "_source": "offline_stub",
    }


def _offline_quote_stub(ticker: str) -> dict:
    """Deterministic hash-based stub. NOT auto-returned."""
    h = int(hashlib.sha256(ticker.encode()).hexdigest()[:6], 16)
    base = 50 + (h % 350)
    return {
        "ticker": ticker,
        "price": float(base),
        "bid": float(base) - 0.05,
        "ask": float(base) + 0.05,
        "volume_today": h % 10_000_000,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source": "offline_stub",
    }


def _fetch_fundamentals_yfinance(ticker: str, yahoo_symbol: str) -> dict:
    yf = _yf()
    if yf is None:
        raise MarketDataError(
            "yfinance", "yfinance not installed — run `pip install yfinance`",
            ticker,
        )
    try:
        t = yf.Ticker(yahoo_symbol)
        info = t.info or {}
    except Exception as e:
        raise MarketDataError(
            "yfinance", f"yfinance call failed: {e}", ticker,
        ) from e
    if not info or not info.get("longName"):
        raise MarketDataError(
            "yfinance",
            "empty/invalid info — ticker may be delisted or unknown",
            ticker,
        )

    pe = info.get("trailingPE") or info.get("forwardPE") or 0
    op_margin = info.get("operatingMargins") or info.get("profitMargins") or 0
    rev_growth = info.get("revenueGrowth") or info.get("earningsGrowth") or 0
    fcf_yield = 0.0
    fcf = info.get("freeCashflow") or 0
    mcap = info.get("marketCap") or 0
    if fcf and mcap:
        fcf_yield = (float(fcf) / float(mcap)) * 100

    _mark_health("yfinance", True)
    return {
        "ticker": ticker,
        "pe_ttm": float(pe or 0),
        "ev_ebitda": float(info.get("enterpriseToEbitda") or 0),
        "fcf_yield_pct": float(fcf_yield),
        "revenue_growth_yoy": float(rev_growth or 0),
        "operating_margin": float(op_margin or 0),
        "next_earnings_in_days": None,
        "market_cap_usd": float(mcap or 0),
        "_source": "yfinance",
        "_yahoo_symbol": yahoo_symbol,
        "_name": info.get("longName", ""),
        "_sector": info.get("sector", ""),
        "_industry": info.get("industry", ""),
        "_business_summary": (info.get("longBusinessSummary") or "")[:1500],
    }


def _fetch_fundamentals_with_fallbacks(ticker: str) -> dict:
    from . import market_providers as mp

    yahoo_sym = mp.normalize_yahoo_symbol(ticker)
    errors: list[str] = []

    try:
        return _fetch_fundamentals_yfinance(ticker, yahoo_sym)
    except MarketDataError as e:
        errors.append(str(e))
        _mark_health("yfinance", False, e.message)

    # EDGAR company facts + live quote for market cap
    cik = _cik_for_ticker(ticker)
    if cik is None:
        raise MarketDataError(
            "market_data",
            f"all sources failed; ticker not in EDGAR map. {' | '.join(errors)}",
            ticker,
        )
    quote_price: Optional[float] = None
    for getter in (
        lambda: fetch_quote_yahoo_chart(yahoo_sym, ticker),
        lambda: mp.fetch_quote_stooq(ticker),
    ):
        try:
            q = getter()
            quote_price = float(q["price"])
            _mark_health(q["_source"], True)
            break
        except (MarketDataError, Exception) as e:
            src = getattr(e, "source", "quote")
            msg = getattr(e, "message", str(e))[:200]
            errors.append(f"{src}: {msg}")
            _mark_health(src if src in _HEALTH else "yahoo_chart", False, msg)

    try:
        fund = mp.fetch_fundamentals_edgar(
            ticker, cik, quote_price=quote_price,
        )
        _mark_health("edgar_facts", True)
        fund["_fallback_chain"] = errors
        return fund
    except MarketDataError as e:
        _mark_health("edgar_facts", False, e.message)
        errors.append(str(e))
        raise MarketDataError(
            "market_data",
            f"all sources failed: {' | '.join(errors)}",
            ticker,
        ) from e


def fetch_quote_yahoo_chart(yahoo_symbol: str, ticker: str) -> dict:
    from . import market_providers as mp
    return mp.fetch_quote_yahoo_chart(yahoo_symbol, ticker)


def _asset_meta(ticker: str):
    from . import asset_universe
    return asset_universe.resolve(ticker)


def _coingecko_enabled() -> bool:
    from . import config
    return config.ENABLE_COINGECKO


def fetch_fundamentals_real(ticker: str) -> dict:
    """Fundamentals routed by asset class (equity / crypto / commodity ETF)."""
    meta = _asset_meta(ticker)

    def fetch():
        from . import market_providers as mp
        if meta.is_crypto:
            if not _coingecko_enabled():
                raise MarketDataError(
                    "coingecko", "CoinGecko disabled (ENABLE_COINGECKO=0)", ticker,
                )
            if not meta.coingecko_id:
                raise MarketDataError(
                    "coingecko", "missing coingecko_id in asset registry", ticker,
                )
            try:
                out = mp.fetch_fundamentals_coingecko(meta.coingecko_id, ticker)
                _mark_health("coingecko", True)
                return out
            except MarketDataError as e:
                _mark_health("coingecko", False, e.message)
                raise
        return _fetch_fundamentals_with_fallbacks(ticker)

    return _cached(
        ("fundamentals", meta.asset_class, ticker),
        ttl=_DEFAULT_TTL_SECONDS,
        fn=fetch,
    )


def fetch_quote_real(ticker: str) -> dict:
    """Quote routed by asset class."""
    from . import market_providers as mp
    meta = _asset_meta(ticker)

    def fetch():
        if meta.is_crypto:
            if not _coingecko_enabled():
                raise MarketDataError(
                    "coingecko", "CoinGecko disabled (ENABLE_COINGECKO=0)", ticker,
                )
            if not meta.coingecko_id:
                raise MarketDataError(
                    "coingecko", "missing coingecko_id in asset registry", ticker,
                )
            try:
                out = mp.fetch_quote_coingecko(meta.coingecko_id, ticker)
                _mark_health("coingecko", True)
                return out
            except MarketDataError as e:
                _mark_health("coingecko", False, e.message)
                raise
        yahoo_sym = mp.normalize_yahoo_symbol(
            meta.yahoo_symbol or ticker,
        )
        errors: list[str] = []

        yf = _yf()
        if yf is not None:
            try:
                t = yf.Ticker(yahoo_sym)
                hist = t.history(period="5d")
                if hist is not None and not hist.empty:
                    last = hist.iloc[-1]
                    price = float(last["Close"])
                    _mark_health("yfinance", True)
                    return {
                        "ticker": ticker,
                        "price": price,
                        "bid": price - 0.05,
                        "ask": price + 0.05,
                        "volume_today": int(last.get("Volume", 0) or 0),
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "_source": "yfinance",
                        "_yahoo_symbol": yahoo_sym,
                    }
            except Exception as e:
                errors.append(f"yfinance: {e}")
                _mark_health("yfinance", False, str(e))

        for fn, src in (
            (lambda: mp.fetch_quote_yahoo_chart(yahoo_sym, ticker), "yahoo_chart"),
            (lambda: mp.fetch_quote_stooq(ticker), "stooq"),
        ):
            try:
                out = fn()
                _mark_health(src, True)
                return out
            except MarketDataError as e:
                errors.append(str(e))
                _mark_health(src, False, e.message)

        raise MarketDataError(
            "market_data", " | ".join(errors) or "no quote source", ticker,
        )

    return _cached(("quote", meta.asset_class, ticker), ttl=60, fn=fetch)


def fetch_news_for_ticker_real(ticker: str, top_k: int = 5) -> dict:
    """News: yfinance → Google News RSS; crypto uses crypto RSS query."""
    from . import market_providers as mp
    meta = _asset_meta(ticker)

    def fetch():
        if meta.is_crypto:
            names = {"BTC": "Bitcoin", "ETH": "Ethereum"}
            coin_name = names.get(ticker.upper(), ticker)
            hits = mp.fetch_news_google_rss_crypto(ticker, coin_name, top_k=top_k)
            if hits:
                _mark_health("google_news", True)
                return hits
            return []
        yahoo_sym = mp.normalize_yahoo_symbol(
            meta.yahoo_symbol or ticker,
        )
        yf = _yf()
        if yf is not None:
            try:
                items = yf.Ticker(yahoo_sym).news or []
                out = []
                for n in items[:top_k]:
                    parsed = mp.parse_yfinance_news_item(n)
                    if parsed.get("title"):
                        out.append(parsed)
                if out:
                    _mark_health("yfinance", True)
                    return out
            except Exception as e:
                _mark_health("yfinance", False, str(e))

        hits = mp.fetch_news_google_rss(ticker, top_k=top_k)
        if hits:
            _mark_health("google_news", True)
            return hits
        return []

    try:
        hits = _cached(
            ("news", ticker, top_k), ttl=_DEFAULT_TTL_SECONDS, fn=fetch,
        )
        if hits:
            src = "yfinance" if hits[0].get("publisher") != "Google News" else "google_news"
            return {"source": src, "hits": hits, "error": None}
        return {"source": "none", "hits": [], "error": None}
    except Exception as e:
        _mark_health("google_news", False, str(e))
        return {"source": "error", "hits": [], "error": str(e)[:200]}


def fetch_macro_context() -> dict:
    """FX snapshot for firm_state / manager (not per-ticker pricing)."""
    from . import config, market_providers as mp

    if not config.ENABLE_FX_CONTEXT:
        return {"enabled": False, "_source": "disabled"}
    try:
        fx = mp.fetch_fx_rates_usd()
        _mark_health("frankfurter", True)
        return {"enabled": True, "fx": fx, "_source": "frankfurter"}
    except MarketDataError as e:
        _mark_health("frankfurter", False, e.message)
        return {"enabled": False, "error": e.message, "_source": "frankfurter"}


def fetch_company_profile(ticker: str) -> dict:
    """Profile from fundamentals chain (any successful source)."""
    meta = _asset_meta(ticker)
    fund = fetch_fundamentals_real(ticker)
    return {
        "ticker": ticker,
        "name": fund.get("_name", meta.ticker),
        "sector": fund.get("_sector", "") or meta.sector or "Unknown",
        "industry": fund.get("_industry", "") or "",
        "market_cap_usd": fund.get("market_cap_usd", 0),
        "business_description": fund.get("_business_summary", meta.blurb),
        "asset_class": meta.asset_class,
        "data_provider": meta.data_provider,
        "_source": fund.get("_source", "market_data"),
    }


# ---------- health ----------

def health_status() -> dict:
    """Snapshot used by /diagnostics. Side-effect-free."""
    from . import config
    return {
        "edgar": dict(_HEALTH["edgar"]),
        "yfinance": dict(_HEALTH["yfinance"]),
        "yahoo_chart": dict(_HEALTH["yahoo_chart"]),
        "stooq": dict(_HEALTH["stooq"]),
        "edgar_facts": dict(_HEALTH["edgar_facts"]),
        "google_news": dict(_HEALTH["google_news"]),
        "coingecko": dict(_HEALTH["coingecko"]),
        "frankfurter": dict(_HEALTH["frankfurter"]),
        "yfinance_installed": _yf() is not None,
        "coingecko_enabled": _coingecko_enabled(),
        "fx_context_enabled": config.ENABLE_FX_CONTEXT,
        "cache_size": len(_CACHE),
    }


def probe_apis(ticker: str = "MSFT") -> dict:
    """Active probe — exercise each source on one ticker."""
    from . import market_providers as mp

    out: dict = {"ticker": ticker, "yahoo_symbol": mp.normalize_yahoo_symbol(ticker)}
    try:
        cm = _edgar_cik_to_ticker_map()
        out["edgar"] = {"ok": True, "ticker_map_size": len(cm)}
    except Exception as e:
        out["edgar"] = {"ok": False, "error": str(e)[:200]}

    out["yfinance_installed"] = _yf() is not None

    try:
        f = fetch_fundamentals_real(ticker)
        out["fundamentals"] = {
            "ok": True, "source": f.get("_source"),
            "market_cap_usd": f.get("market_cap_usd"),
            "pe_ttm": f.get("pe_ttm"),
        }
    except MarketDataError as e:
        out["fundamentals"] = {"ok": False, "error": str(e)[:200]}

    try:
        q = fetch_quote_real(ticker)
        out["quote"] = {"ok": True, "source": q.get("_source"), "price": q.get("price")}
    except MarketDataError as e:
        out["quote"] = {"ok": False, "error": str(e)[:200]}

    news = fetch_news_for_ticker_real(ticker, top_k=2)
    out["news"] = {
        "ok": news.get("source") not in ("error",),
        "source": news.get("source"),
        "count": len(news.get("hits") or []),
        "error": news.get("error"),
    }

    # Class-share symbol check
    if ticker.upper() == "BRK.B" or ticker.upper() == "BRK-B":
        out["symbol_hint"] = (
            "Use BRK-B (not BRK.B) for Yahoo/yfinance — auto-normalized in code."
        )

    # Crypto probe when enabled
    if _coingecko_enabled():
        try:
            cq = fetch_quote_real("BTC")
            out["crypto_btc"] = {
                "ok": True, "source": cq.get("_source"), "price": cq.get("price"),
            }
        except MarketDataError as e:
            out["crypto_btc"] = {"ok": False, "error": str(e)[:200]}

    out["health"] = health_status()
    return out
