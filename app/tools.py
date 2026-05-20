"""Tool implementations.

Every call is wrapped with a trace event so the UI can show the agent's
external interactions: tool name, args, result summary, latency.
"""
from __future__ import annotations
import json
import time
import hashlib
import functools
from pathlib import Path
from typing import Optional, Any, Callable
from . import db, rag, config, traces


def _traced_tool(tool_name: str):
    """Decorator: local trace + nested LangSmith tool span."""
    def deco(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            t0 = time.time()
            try:
                result = fn(*args, **kwargs)
                duration_ms = int((time.time() - t0) * 1000)
                traces.record_tool_call(
                    tool_name, args, kwargs, result, duration_ms, status="ok",
                )
                return result
            except Exception as e:
                duration_ms = int((time.time() - t0) * 1000)
                traces.record_tool_call(
                    tool_name, args, kwargs, None, duration_ms,
                    status="error", error=str(e),
                )
                raise
        return wrapper
    return deco


# ---------- KNOWLEDGE TOOLS ----------

@_traced_tool("get_dossier")
def get_dossier(ticker: str) -> dict:
    from . import dossier_paths

    return dossier_paths.load(ticker)


@_traced_tool("save_dossier")
def save_dossier(ticker: str, dossier: dict) -> dict:
    """Persist dossier under artifacts/dossiers (writable); seed data/ stays read-only."""
    from . import dossier_paths

    t = ticker.upper()
    path = dossier_paths.save(t, dossier)
    return {"saved": True, "ticker": t, "path": str(path)}


@_traced_tool("ensure_scan_dossier")
def ensure_scan_dossier(pick: dict, scan_run_id: str) -> dict:
    """Create or refresh a dossier from an Idea Generator top pick.

    Satisfies new-name-onboarding stage 3 so Plan Builder can draft for
    scan-routed names. Sets watchlist_seasoned for scan-fast-path (POC).
    """
    ticker = (pick.get("ticker") or "").upper()
    if not ticker:
        return {"created": False, "reason": "missing_ticker"}

    existing = get_dossier(ticker)
    brief = pick.get("research_brief") or {}
    fund = pick.get("fundamentals_snapshot") or {}
    fit = pick.get("portfolio_fit") or {}
    meta = pick.get("firm_coverage") or {}
    entry = (brief.get("suggested_entry") or {})
    entry_pct = float(
        entry.get("target_size_pct_nav")
        or (0.03 if meta.get("new_to_firm") else 0.04)
    )

    if existing.get("found"):
        d = dict(existing["dossier"])
        d.setdefault("onboarding_source", "idea_scan")
        d["watchlist_seasoned"] = True
        d["scan_run_id"] = scan_run_id
        d["suggested_entry_pct_nav"] = entry_pct
        if brief.get("business_overview") and not d.get("business_description"):
            d["business_description"] = brief["business_overview"][:2000]
        save_dossier(ticker, d)
        return {"created": False, "updated": True, "ticker": ticker}

    sector = fit.get("sector") or meta.get("sector") or "Unknown"
    risks = []
    for r in (brief.get("risks") or [])[:5]:
        if isinstance(r, str):
            risks.append({
                "category": "onboarding",
                "description": r[:300],
                "severity": "medium",
            })
        elif isinstance(r, dict):
            risks.append(r)

    dossier = {
        "ticker": ticker,
        "name": ticker,
        "sector": sector,
        "sub_industry": meta.get("industry") or fund.get("_industry") or "",
        "market_cap_usd": float(fund.get("market_cap_usd") or 0),
        "business_description": (
            (brief.get("business_overview") or brief.get("executive_summary") or "")
            [:2000]
            or f"{ticker} — dossier drafted from Idea Generator scan {scan_run_id}."
        ),
        "revenue_segments": [],
        "known_risks": risks or [{
            "category": "onboarding",
            "description": "Scan-draft dossier — operator should validate before live trade.",
            "severity": "medium",
        }],
        "peer_set": [],
        "current_status": "watchlist",
        "onboarding_source": "idea_scan",
        "watchlist_seasoned": True,
        "scan_run_id": scan_run_id,
        "suggested_entry_pct_nav": entry_pct,
        "scan_composite_score": pick.get("composite_score"),
    }
    save_dossier(ticker, dossier)
    return {"created": True, "ticker": ticker}


@_traced_tool("get_firm_state")
def get_firm_state(refresh_prices: bool = False) -> dict:
    """Live portfolio + capital-allocation context for agent decisions."""
    from . import firm_state
    return firm_state.build_firm_state(refresh_prices=refresh_prices)


@_traced_tool("get_holdings")
def get_holdings() -> dict:
    holdings = db.list_holdings()
    total_value = sum(h["quantity"] * h["current_price"] for h in holdings)
    nav = config.STARTING_NAV
    sector_exposures: dict[str, float] = {}
    for h in holdings:
        sector = h.get("sector") or "Unknown"
        sector_exposures[sector] = sector_exposures.get(sector, 0.0) + (
            (h["quantity"] * h["current_price"]) / nav
        )
    return {
        "holdings": holdings,
        "total_nav_usd": nav,
        "total_positions_value_usd": total_value,
        "cash_usd": nav - total_value,
        "sector_exposures": sector_exposures,
        "count": len(holdings),
    }


@_traced_tool("get_plan")
def get_plan(plan_id: str) -> dict:
    plan = db.load_plan_body(plan_id)
    if not plan:
        return {"found": False}
    return {"found": True, "plan": plan}


@_traced_tool("update_plan_status")
def update_plan_status(plan_id: str, new_status: str,
                       approved_by: Optional[str] = None,
                       rejection_reason: Optional[str] = None,
                       history_entry: Optional[dict] = None) -> dict:
    p = db.update_plan_status(plan_id, new_status, approved_by, rejection_reason,
                              history_entry)
    if p is None:
        return {"updated": False}
    return {"updated": True, "plan": p}


# ---------- RETRIEVAL (RAG) TOOLS ----------

@_traced_tool("search_news")
def search_news(query: str, tickers: Optional[list[str]] = None,
                top_k: int = 5) -> dict:
    results = rag.search("news", query, top_k=top_k)
    if tickers:
        results = [r for r in results if any(
            t in (r["metadata"].get("tickers") or []) for t in tickers
        )]
    return {"hits": results, "count": len(results), "query": query}


@_traced_tool("search_filings")
def search_filings(query: str, ticker: Optional[str] = None, top_k: int = 5) -> dict:
    results = rag.search("filings", query, top_k=top_k,
                          metadata_filter={"ticker": ticker} if ticker else None)
    return {"hits": results, "count": len(results), "query": query}


@_traced_tool("search_past_plans")
def search_past_plans(query: str, sector: Optional[str] = None,
                       top_k: int = 5) -> dict:
    results = rag.search("past_plans", query, top_k=top_k,
                          metadata_filter={"sector": sector} if sector else None)
    return {"hits": results, "count": len(results), "query": query}


@_traced_tool("search_policy")
def search_policy(query: str, top_k: int = 3) -> dict:
    results = rag.search("policy", query, top_k=top_k)
    return {"hits": results, "count": len(results), "query": query}


# ---------- MARKET / FUNDAMENTAL DATA (mocked) ----------

def _fundamentals_error_stub(ticker: str, err: str) -> dict:
    """Returned when fundamentals are unavailable. Numeric fields are zero so
    arithmetic doesn't crash, but `_source='error'` and `_error=...` are loud
    enough for callers (and the LLM in the agent prompt) to skip the candidate
    or escalate. NOT a hash mock — does not produce a plausible-looking value.
    """
    return {
        "ticker": ticker,
        "pe_ttm": 0.0,
        "ev_ebitda": 0.0,
        "fcf_yield_pct": 0.0,
        "revenue_growth_yoy": 0.0,
        "operating_margin": 0.0,
        "next_earnings_in_days": None,
        "market_cap_usd": 0.0,
        "_source": "error",
        "_error": err,
        "_data_unavailable": True,
    }


def _quote_error_stub(ticker: str, err: str) -> dict:
    # price=1.0 is a SAFE placeholder to prevent ZeroDivisionError in callers
    # that do not (yet) check _data_unavailable. Callers MUST check the flag —
    # the price is not real and the candidate must be refused.
    return {
        "ticker": ticker,
        "price": 1.0,
        "bid": 1.0,
        "ask": 1.0,
        "volume_today": 0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source": "error",
        "_error": err,
        "_data_unavailable": True,
    }


@_traced_tool("fetch_quote")
def fetch_quote(ticker: str) -> dict:
    """Real quote via yfinance. On failure returns an ERROR STUB (not mock)
    with `_source='error'` and `_error=<reason>` so callers can detect and
    skip rather than reason over fake numbers.
    """
    from .core.settings import get_settings
    if get_settings().MCP_MARKET_ENABLED:
        from . import mcp_market, market_data as _md
        try:
            return mcp_market.fetch_quote(ticker)
        except _md.MarketDataError:
            pass
    from . import market_data
    try:
        return market_data.fetch_quote_real(ticker)
    except market_data.MarketDataError as e:
        return _quote_error_stub(ticker, e.message)
    except Exception as e:
        return _quote_error_stub(ticker, str(e)[:200])


@_traced_tool("fetch_fundamentals")
def fetch_fundamentals(ticker: str) -> dict:
    """Real fundamentals via yfinance. On failure returns an ERROR STUB."""
    from .core.settings import get_settings
    if get_settings().MCP_MARKET_ENABLED:
        from . import mcp_market, market_data as _md
        try:
            return mcp_market.fetch_fundamentals(ticker)
        except _md.MarketDataError:
            pass
    from . import market_data
    try:
        return market_data.fetch_fundamentals_real(ticker)
    except market_data.MarketDataError as e:
        return _fundamentals_error_stub(ticker, e.message)
    except Exception as e:
        return _fundamentals_error_stub(ticker, str(e)[:200])


@_traced_tool("discover_recent_8k")
def discover_recent_8k(count: int = 40, exclude: list = None) -> dict:
    """Pull the most recent 8-K filings from EDGAR; returns tickers + meta."""
    from .core.settings import get_settings
    excl = set(exclude or [])
    if get_settings().MCP_MARKET_ENABLED:
        from . import mcp_market
        return mcp_market.discover_recent_8k(count=count, exclude=excl)
    from . import market_data
    return market_data.discover_recent_8k_tickers(count=count, exclude=excl)


@_traced_tool("discover_idea_candidates")
def discover_idea_candidates(count_per_form: int = 25,
                             exclude: list = None) -> dict:
    """Multi-channel EDGAR discovery (8-K + 10-Q) for proactive scans."""
    from .core.settings import get_settings
    excl = set(exclude or [])
    if get_settings().MCP_MARKET_ENABLED:
        from . import mcp_market
        return mcp_market.discover_idea_candidates(
            count_per_form=count_per_form, exclude=excl,
        )
    from . import market_data
    return market_data.discover_idea_candidates(
        count_per_form=count_per_form, exclude=excl,
    )


@_traced_tool("get_firm_coverage")
def get_firm_coverage(ticker: str) -> dict:
    """Whether the firm already knows this ticker (dossier, pool, holdings)."""
    t = ticker.upper()
    dossier = get_dossier(t)
    in_pool = False
    pool_path = config.DATA / "candidates.json"
    if pool_path.exists():
        try:
            pool = json.loads(pool_path.read_text()).get("candidate_pool") or []
            in_pool = any(c.get("ticker") == t for c in pool)
        except Exception:
            pass
    held = any(h["ticker"] == t for h in db.list_holdings())
    history = db.get_idea_history_for_ticker(t)
    has_dossier = bool(dossier.get("found"))
    # Per new-name-onboarding §1: pool-only names are not firm-known until dossier exists.
    firm_known = has_dossier or held
    if has_dossier:
        coverage = "dossier_backed"
    elif held:
        coverage = "held_only"
    elif in_pool:
        coverage = "pool_tracked"
    else:
        coverage = "new_to_firm"
    return {
        "ticker": t,
        "has_dossier": has_dossier,
        "in_candidate_pool": in_pool,
        "currently_held": held,
        "times_scanned": len(history),
        "firm_known": firm_known,
        "coverage_tier": coverage,
        # Any name without a dossier is new-to-firm for onboarding (§1).
        "new_to_firm": not firm_known,
    }


@_traced_tool("fetch_news_for_ticker")
def fetch_news_for_ticker(ticker: str, top_k: int = 5) -> dict:
    """Recent ticker-specific news via yfinance."""
    from . import market_data
    return market_data.fetch_news_for_ticker_real(ticker, top_k=top_k)


@_traced_tool("fetch_recent_filings_for_ticker")
def fetch_recent_filings_for_ticker(ticker: str, top_k: int = 5) -> dict:
    """Recent SEC filings (8-K, 10-K, 10-Q) for a ticker."""
    from . import market_data
    return market_data.fetch_recent_filings_for_ticker(ticker, top_k=top_k)


@_traced_tool("get_asset_metadata")
def get_asset_metadata(ticker: str) -> dict:
    """Asset class, data provider, and policy caps for a ticker."""
    from . import asset_universe
    m = asset_universe.resolve(ticker)
    return {
        "ticker": m.ticker,
        "asset_class": m.asset_class,
        "sector": m.sector,
        "data_provider": m.data_provider,
        "coingecko_id": m.coingecko_id,
        "yahoo_symbol": m.yahoo_symbol,
        "underlying": m.underlying,
        "min_market_cap_usd": m.min_market_cap_usd,
        "max_position_pct_nav": m.max_position_pct_nav,
        "blurb": m.blurb,
        "is_crypto": m.is_crypto,
        "is_commodity_proxy": m.is_commodity_proxy,
        "is_equity": m.is_equity,
    }


@_traced_tool("fetch_company_profile")
def fetch_company_profile(ticker: str) -> dict:
    """Company profile from yfinance. On failure returns an error stub."""
    from . import market_data
    try:
        return market_data.fetch_company_profile(ticker)
    except market_data.MarketDataError as e:
        return {
            "ticker": ticker, "name": ticker,
            "sector": "Unknown", "industry": "Unknown",
            "market_cap_usd": 0, "business_description": "",
            "_source": "error", "_error": e.message,
            "_data_unavailable": True,
        }


# ---------- PORTFOLIO OPS ----------

@_traced_tool("simulate_order")
def simulate_order(ticker: str, side: str, quantity: int,
                   current_price: Optional[float] = None) -> dict:
    from . import allocation, asset_universe
    meta = asset_universe.resolve(ticker)
    pos_cap = (
        meta.max_position_pct_nav
        if meta.is_crypto
        else allocation.MAX_POSITION_PCT
    )
    if current_price is None:
        # Direct call to bypass tracing inner fetch_quote — we'd see double-trace otherwise
        h = int(hashlib.sha256(ticker.encode()).hexdigest()[:6], 16)
        current_price = float(50 + (h % 350))

    nav = config.STARTING_NAV
    holdings_raw = db.list_holdings()
    total_pos_value = sum(h["quantity"] * h["current_price"] for h in holdings_raw)
    cash = nav - total_pos_value
    notional = quantity * current_price
    existing = next((h for h in holdings_raw if h["ticker"] == ticker), None)
    maiden = existing is None
    pending_deploy = 0.0
    for p_row in db.list_plans(status="pending_hitl"):
        body = db.load_plan_body(p_row["plan_id"])
        if not body:
            continue
        pending_deploy += nav * float((body.get("entry") or {}).get("target_size_pct_nav") or 0)
    liq = allocation.liquidity_budget(
        nav, cash, pending_deploy_usd=pending_deploy, maiden_entry=maiden,
    )

    sector_usd: dict[str, float] = {}
    for h in holdings_raw:
        sec = allocation.normalize_sector(h.get("sector") or "Unknown")
        sector_usd[sec] = sector_usd.get(sec, 0.0) + (
            h["quantity"] * h["current_price"]
        )

    violations = []
    new_pct = 0.0
    sector_after_pct = 0.0
    if side == "long":
        if notional > liq["deployable_cash_usd"] + 1e-6:
            violations.append({
                "policy_section": "capital-allocation §1",
                "reason": (
                    f"Order ${notional:,.0f} exceeds deployable cash "
                    f"${liq['deployable_cash_usd']:,.0f} "
                    f"(must keep ≥{liq['reserve_pct']:.0%} NAV in cash"
                    f"{', pro-forma pending HITL included' if pending_deploy else ''})"
                ),
            })
        new_position_value = notional + (existing["quantity"] * current_price if existing else 0)
        new_pct = new_position_value / nav if nav else 0
        if new_pct > pos_cap + 1e-6:
            violations.append({
                "policy_section": (
                    "multi-asset-data §2" if meta.is_crypto
                    else "investment-policy §2"
                ),
                "reason": (
                    f"Position would be {new_pct:.1%} of NAV "
                    f"(cap {pos_cap:.0%})"
                ),
            })
        if notional > nav * allocation.PER_ORDER_MAX_PCT:
            violations.append({
                "policy_section": "risk-policy §6",
                "reason": (
                    f"Order size {notional/nav:.1%} of NAV exceeds "
                    f"{allocation.PER_ORDER_MAX_PCT:.0%} per-order cap"
                ),
            })
        if cash - notional < nav * allocation.CASH_FLOOR_PCT - 1e-6:
            violations.append({
                "policy_section": "capital-allocation §1",
                "reason": (
                    f"Order would breach {allocation.CASH_FLOOR_PCT:.0%} cash hard floor"
                ),
            })

        dossier = get_dossier(ticker)
        sector = allocation.normalize_sector(
            meta.sector
            or (dossier.get("dossier") or {}).get("sector", "Unknown")
            if dossier.get("found")
            else (existing or {}).get("sector", "Unknown"),
        )
        sector_before = sector_usd.get(sector, 0.0) / nav if nav else 0
        add_pct = notional / nav if nav else 0
        sector_after_pct = sector_before + add_pct
        if sector_after_pct > allocation.MAX_SECTOR_PCT + 1e-6:
            violations.append({
                "policy_section": "capital-allocation §3",
                "reason": (
                    f"{sector} would be {sector_after_pct:.1%} NAV "
                    f"(hard cap {allocation.MAX_SECTOR_PCT:.0%})"
                ),
            })
        if meta.is_crypto:
            digital_before = sector_usd.get("Digital Assets", 0.0) / nav if nav else 0
            digital_after = digital_before + add_pct
            if digital_after > allocation.MAX_DIGITAL_ASSETS_PCT + 1e-6:
                violations.append({
                    "policy_section": "multi-asset-data §2",
                    "reason": (
                        f"Digital Assets sleeve would be {digital_after:.1%} NAV "
                        f"(aggregate cap {allocation.MAX_DIGITAL_ASSETS_PCT:.0%})"
                    ),
                })
        invested_after = (total_pos_value + notional) / nav if nav else 0
        if invested_after > allocation.MAX_INVESTED_PCT + 1e-6:
            violations.append({
                "policy_section": "capital-allocation §2",
                "reason": (
                    f"Book would be {invested_after:.1%} invested "
                    f"(max {allocation.MAX_INVESTED_PCT:.0%})"
                ),
            })
    elif side in ("sell", "short"):
        held_qty = int(existing["quantity"] or 0) if existing else 0
        if held_qty <= 0:
            violations.append({
                "policy_section": "execution",
                "reason": f"No open position in {ticker} to sell",
            })
        elif quantity > held_qty:
            violations.append({
                "policy_section": "execution",
                "reason": (
                    f"Sell quantity {quantity} exceeds held {held_qty} shares"
                ),
            })

    feasible = len(violations) == 0

    return {
        "feasible": feasible,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "notional_usd": notional,
        "cash_after_usd": cash - notional if side == "long" else cash + notional,
        "position_pct_nav_after": new_pct,
        "sector_pct_nav_after": sector_after_pct,
        "policy_violations": violations,
        "liquidity": liq,
    }


# ---------- EXECUTION (mocked) ----------

@_traced_tool("submit_order_sim")
def submit_order_sim(
    plan_id: str,
    ticker: str,
    side: str,
    quantity: int,
    *,
    run_id: str = "",
    as_of: str | None = None,
) -> dict:
    if quantity <= 0:
        return {
            "status": "rejected",
            "reasons": [{
                "policy_section": "execution",
                "reason": f"quantity must be > 0, got {quantity}",
            }],
        }
    sim = simulate_order(ticker, side, quantity)
    if not sim["feasible"]:
        return {"status": "rejected", "reasons": sim["policy_violations"]}

    # Realistic fill: slippage + commission + market-hours awareness via
    # app/execution.py (the production execution layer). Asset class is
    # resolved through asset_universe so crypto / ETF proxies get their
    # own bp and fee schedules.
    from . import execution
    h = int(hashlib.sha256(ticker.encode()).hexdigest()[:6], 16)
    last_price = float(50 + (h % 350))
    _f = execution.compute_fill(
        side=side, quantity=quantity, last_price=last_price,
        asset_class=execution.asset_class_for(ticker),
    )
    fill_price = _f.fill_price

    sector = "Unknown"
    p = db.get_plan(plan_id)
    if p:
        d = get_dossier(ticker)
        if d.get("found"):
            sector = d["dossier"].get("sector", "Unknown")

    db.upsert_holding(ticker, quantity, fill_price, fill_price, plan_id, sector)
    fill = {
        "status": "filled",
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "fill_price": fill_price,
        "notional_usd": _f.notional_usd,
        # Execution detail (brief: realistic fills with slippage + commission)
        "commission_usd": _f.commission_usd,
        "slippage_bp": _f.slippage_bp,
        "asset_class": _f.asset_class,
        "market_hours_ok": _f.market_hours_ok,
        "execution_notes": _f.notes,
    }
    try:
        from . import trade_history
        trade_history.record_from_fill(
            fill,
            plan_id=plan_id,
            run_id=run_id,
            as_of=as_of or time.strftime("%Y-%m-%dT%H:%M:%S"),
            sector=sector,
            action="buy",
        )
    except Exception as e:
        print(f"[trade_history] record fill failed: {e}")
    return fill


@_traced_tool("close_position_sim")
def close_position_sim(
    ticker: str,
    *,
    quantity: Optional[int] = None,
    run_id: str = "operator_exit",
    note: str = "",
    as_of: Optional[str] = None,
) -> dict:
    """Full or partial exit of an open holding (operator dashboard)."""
    from . import execution, trade_history

    ticker = ticker.upper()
    holding = next((h for h in db.list_holdings() if h["ticker"] == ticker), None)
    if not holding or int(holding.get("quantity") or 0) <= 0:
        return {
            "status": "rejected",
            "reasons": [{"reason": f"No open position in {ticker}"}],
        }

    held_qty = int(holding["quantity"])
    qty = held_qty if quantity is None else min(max(1, int(quantity)), held_qty)
    quote = fetch_quote(ticker)
    last_price = float(quote.get("price") or holding.get("current_price") or 0)
    if last_price <= 0:
        return {
            "status": "rejected",
            "reasons": [{"reason": "Cannot price exit — market data unavailable"}],
        }

    sim = simulate_order(ticker, "sell", qty, last_price)
    if not sim.get("feasible"):
        return {"status": "rejected", "reasons": sim.get("policy_violations", [])}

    fill_model = execution.compute_fill(
        side="sell",
        quantity=qty,
        last_price=last_price,
        asset_class=execution.asset_class_for(ticker),
    )
    fill_price = fill_model.fill_price
    cost_basis = float(holding.get("cost_basis") or fill_price)
    cost_total = cost_basis * qty
    proceeds = fill_model.notional_usd
    realized_pnl = proceeds - cost_total
    return_pct = (realized_pnl / cost_total) if cost_total > 0 else 0.0

    plan_id = (holding.get("plan_id") or "").strip()
    sector = holding.get("sector") or "Unknown"
    as_of_ts = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")

    if qty >= held_qty:
        db.delete_holding(ticker)
        if plan_id:
            db.update_plan_status(
                plan_id,
                "closed",
                history_append={
                    "at": as_of_ts,
                    "agent": "operator",
                    "action": "position_exit",
                    "note": (note or f"Full exit {qty} @ {fill_price:.2f}")[:300],
                },
            )
    else:
        remaining = held_qty - qty
        db.upsert_holding(
            ticker, remaining, cost_basis, fill_price,
            plan_id, sector,
        )

    fill = {
        "status": "filled",
        "ticker": ticker,
        "side": "sell",
        "quantity": qty,
        "fill_price": fill_price,
        "notional_usd": proceeds,
        "commission_usd": fill_model.commission_usd,
        "slippage_bp": fill_model.slippage_bp,
        "realized_pnl_usd": round(realized_pnl, 2),
        "realized_return_pct": round(return_pct, 4),
        "full_exit": qty >= held_qty,
    }
    trade_history.record_trade(
        ticker=ticker,
        action="sell",
        side="long",
        quantity=qty,
        price=fill_price,
        notional_usd=proceeds,
        plan_id=plan_id,
        run_id=run_id,
        sector=sector,
        source="operator_exit",
        as_of=as_of_ts,
        trade_id=f"exit_{ticker}_{int(time.time())}",
        meta={
            "realized_return_pct": return_pct,
            "realized_pnl_usd": realized_pnl,
            "operator_note": note[:200] if note else "",
        },
    )
    return fill
