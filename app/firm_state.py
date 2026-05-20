"""Live firm portfolio snapshot for agent decision-making."""
from __future__ import annotations

from typing import Any, Optional

from . import allocation, db, portfolio, tools


def build_firm_state(refresh_prices: bool = False) -> dict[str, Any]:
    """Current book + allocation policy context for agents."""
    pf = portfolio.get_portfolio_summary(refresh_prices=refresh_prices)
    nav = float(pf["nav_usd"])
    cash_pct = float(pf["cash_pct"])
    invested_pct = float(pf["invested_pct"])
    pending_pct = (float(pf["pending_deploy_usd"]) / nav) if nav else 0.0

    sector_usd: dict[str, float] = {}
    for p in pf["positions"]:
        sec = allocation.normalize_sector(p.get("sector") or "Unknown")
        sector_usd[sec] = sector_usd.get(sec, 0.0) + float(p["market_value"])

    sector_rows: list[dict] = []
    for sec, usd in sorted(sector_usd.items(), key=lambda x: -x[1]):
        pct = usd / nav if nav else 0.0
        band = allocation.sector_band(sec)
        sector_rows.append({
            "sector": sec,
            "usd": round(usd, 2),
            "pct_nav": round(pct, 4),
            "target_pct": band["target_pct"],
            "band_low_pct": band["band_low_pct"],
            "band_high_pct": band["band_high_pct"],
            "headroom_to_cap_pct": round(max(0.0, allocation.MAX_SECTOR_PCT - pct), 4),
        })

    # All strategic sectors (including zero weight)
    seen = {r["sector"] for r in sector_rows}
    for sec, tgt in allocation.SECTOR_TARGETS.items():
        if sec not in seen:
            band = allocation.sector_band(sec)
            sector_rows.append({
                "sector": sec,
                "usd": 0.0,
                "pct_nav": 0.0,
                "target_pct": band["target_pct"],
                "band_low_pct": band["band_low_pct"],
                "band_high_pct": band["band_high_pct"],
                "headroom_to_cap_pct": allocation.MAX_SECTOR_PCT,
            })

    holdings_tickers = [p["ticker"] for p in pf["positions"]]
    positions_compact = [
        {
            "ticker": p["ticker"],
            "sector": allocation.normalize_sector(p.get("sector") or "Unknown"),
            "pct_nav": round(p["pct_nav"], 4),
            "market_value_usd": round(p["market_value"], 2),
            "unrealized_pnl_pct": round(p["unrealized_pnl_pct"], 4),
            "plan_id": p.get("plan_id"),
        }
        for p in pf["positions"]
    ]

    hints = allocation.portfolio_decision_hints(
        cash_pct, invested_pct, len(positions_compact), sector_rows, pending_pct,
        positions=positions_compact,
    )

    macro_context: dict = {}
    try:
        from . import market_data
        macro_context = market_data.fetch_macro_context()
        if macro_context.get("enabled") and macro_context.get("fx"):
            rates = macro_context["fx"].get("rates") or {}
            eur = rates.get("EUR")
            if eur:
                hints.append(f"FX context USD/EUR≈{eur:.4f} (Frankfurter)")
    except Exception:
        pass

    pending_deploy_usd = float(pf.get("pending_deploy_usd") or 0)
    liquidity = allocation.liquidity_budget(
        nav,
        float(pf["cash_usd"]),
        pending_deploy_usd=pending_deploy_usd,
        maiden_entry=True,
    )

    deploy = deployment_needs({
        "invested_pct": invested_pct,
        "cash_pct": cash_pct,
        "positions_count": len(positions_compact),
        "liquidity": liquidity,
        "policy": {
            "min_invested_pct": allocation.MIN_INVESTED_PCT,
            "cash_ceiling_pct": allocation.CASH_CEILING_PCT,
            "cash_target_pct": allocation.CASH_TARGET_PCT,
            "min_position_count": allocation.TARGET_POSITION_COUNT[0],
        },
    })

    from . import trading_posture
    posture = trading_posture.derive_posture({
        "invested_pct": invested_pct,
        "cash_pct": cash_pct,
        "positions_count": len(positions_compact),
        "policy": {
            "min_invested_pct": allocation.MIN_INVESTED_PCT,
            "cash_ceiling_pct": allocation.CASH_CEILING_PCT,
            "min_position_count": allocation.TARGET_POSITION_COUNT[0],
            "max_invested_pct": allocation.MAX_INVESTED_PCT,
            "cash_floor_pct": allocation.CASH_FLOOR_PCT,
        },
        "deployment_needs": deploy,
        "sectors": sector_rows,
        "concentration": allocation.concentrated_positions(positions_compact),
        "positions": positions_compact,
    })

    return {
        "as_of": pf.get("as_of"),
        "nav_usd": nav,
        "cash_usd": float(pf["cash_usd"]),
        "cash_pct": round(cash_pct, 4),
        "invested_usd": float(pf["invested_usd"]),
        "invested_pct": round(invested_pct, 4),
        "deployable_cash_usd": liquidity["deployable_cash_usd"],
        "liquidity": liquidity,
        "pending_hitl_deploy_pct_nav": round(pending_pct, 4),
        "pending_hitl_plans": pf.get("pending_plans") or [],
        "positions_count": len(positions_compact),
        "holdings_tickers": holdings_tickers,
        "positions": positions_compact,
        "sectors": sorted(sector_rows, key=lambda r: -r["pct_nav"]),
        "policy": {
            "cash_floor_pct": allocation.CASH_FLOOR_PCT,
            "cash_target_pct": allocation.CASH_TARGET_PCT,
            "cash_ceiling_pct": allocation.CASH_CEILING_PCT,
            "target_invested_pct": allocation.TARGET_INVESTED_PCT,
            "min_invested_pct": allocation.MIN_INVESTED_PCT,
            "max_invested_pct": allocation.MAX_INVESTED_PCT,
            "max_sector_pct": allocation.MAX_SECTOR_PCT,
            "max_position_pct": allocation.MAX_POSITION_PCT,
            "position_warn_pct": allocation.position_warn_pct(),
            "per_order_max_pct": allocation.PER_ORDER_MAX_PCT,
            "target_position_count": list(allocation.TARGET_POSITION_COUNT),
            "min_position_count": allocation.TARGET_POSITION_COUNT[0],
        },
        "concentration": allocation.concentrated_positions(positions_compact),
        "decision_hints": hints,
        "unrealized_pnl_pct": round(float(pf["unrealized_pnl_pct"]), 4),
        "macro_context": macro_context,
        "deployment_needs": deploy,
        "trading_posture": posture,
    }


def deployment_needs(firm_state: dict) -> dict[str, Any]:
    """Whether the book needs more deployment and/or name diversification."""
    policy = firm_state.get("policy") or {}
    invested = float(firm_state.get("invested_pct", 0))
    cash_pct = float(firm_state.get("cash_pct", 0))
    pos_n = int(firm_state.get("positions_count", 0))
    min_names = int(policy.get("min_position_count", 10))
    min_invested = float(policy.get("min_invested_pct", 0.70))
    cash_ceiling = float(policy.get("cash_ceiling_pct", 0.20))
    cash_target = float(policy.get("cash_target_pct", allocation.CASH_TARGET_PCT))
    liq = firm_state.get("liquidity") or {}
    liquidity_stressed = liq.get("status") == "below_cash_target" or cash_pct < cash_target
    need_deploy = (
        (invested < min_invested or cash_pct > cash_ceiling)
        and not liquidity_stressed
        and liq.get("can_open_new_name", True)
    )
    need_diversify = pos_n < min_names
    return {
        "active": need_deploy or need_diversify,
        "need_deploy": need_deploy,
        "need_diversify": need_diversify,
        "liquidity_stressed": liquidity_stressed,
        "invested_pct": invested,
        "cash_pct": cash_pct,
        "positions_count": pos_n,
        "min_position_count": min_names,
        "deployable_cash_usd": liq.get("deployable_cash_usd"),
    }


def ticker_context(firm_state: dict, ticker: str, proposed_entry_pct: float = 0.04) -> dict:
    """Per-name slice: held?, sector, headroom for proposed entry."""
    ticker = ticker.upper()
    held = next(
        (p for p in firm_state.get("positions", []) if p["ticker"] == ticker),
        None,
    )
    from . import tools
    dossier = tools.get_dossier(ticker)
    sector = allocation.normalize_sector(
        (dossier.get("dossier") or {}).get("sector", "Unknown") if dossier.get("found")
        else (held or {}).get("sector", "Unknown"),
    )
    current_sector_pct = 0.0
    for row in firm_state.get("sectors", []):
        if row["sector"] == sector:
            current_sector_pct = float(row["pct_nav"])
            break
    headroom = allocation.analyze_sector_headroom(
        sector, current_sector_pct, proposed_entry_pct,
    )
    return {
        "ticker": ticker,
        "held": held is not None,
        "current_position_pct_nav": float((held or {}).get("pct_nav") or 0),
        "sector": sector,
        "sector_headroom": headroom,
        "is_maiden": held is None,
    }


def format_for_prompt(firm_state: dict, ticker: Optional[str] = None,
                      proposed_entry_pct: float = 0.04) -> str:
    """Compact text block for LLM user messages."""
    import json
    lines = [
        f"NAV ${firm_state['nav_usd']:,.0f} | "
        f"cash {firm_state['cash_pct']:.1%} (floor {firm_state['policy']['cash_floor_pct']:.0%}, "
        f"target {firm_state['policy']['cash_target_pct']:.0%}) | "
        f"invested {firm_state['invested_pct']:.1%} | "
        f"{firm_state['positions_count']} positions",
    ]
    if firm_state.get("pending_hitl_deploy_pct_nav", 0) > 0:
        lines.append(
            f"Pending HITL deploy: {firm_state['pending_hitl_deploy_pct_nav']:.1%} NAV"
        )
    liq = firm_state.get("liquidity") or {}
    if liq:
        lines.append(
            f"Liquidity: deployable ${liq.get('deployable_cash_usd', 0):,.0f} "
            f"({liq.get('deployable_pct_nav', 0):.1%} NAV) — "
            f"reserve {liq.get('reserve_pct', 0):.0%}; "
            f"max maiden entry {liq.get('max_maiden_entry_pct_nav', 0):.1%}; "
            f"≤{liq.get('max_new_maiden_entries', 0)} new names at 3% each"
        )
    if firm_state.get("decision_hints"):
        lines.append("Hints: " + "; ".join(firm_state["decision_hints"][:4]))
    deploy = firm_state.get("deployment_needs") or {}
    if deploy.get("active"):
        lines.append(
            f"DEPLOYMENT NEEDS: active — invested {deploy.get('invested_pct', 0):.1%}, "
            f"{deploy.get('positions_count', 0)} names (min "
            f"{deploy.get('min_position_count', 10)}); "
            f"bias Idea Scan toward new names in underweight sectors."
        )
    posture = firm_state.get("trading_posture")
    if posture:
        lines.append(posture.get("summary", ""))
        risk_g = (posture.get("agent_guidance") or {}).get("risk_officer", "")
        if risk_g:
            lines.append(f"Risk posture: {risk_g[:220]}")
    if firm_state.get("positions"):
        pos = ", ".join(
            f"{p['ticker']} {p['pct_nav']:.1%}" for p in firm_state["positions"][:12]
        )
        lines.append(f"Holdings: {pos}")
    top_sectors = sorted(
        firm_state.get("sectors", []), key=lambda r: -r["pct_nav"],
    )[:6]
    if top_sectors:
        sec = ", ".join(
            f"{r['sector']} {r['pct_nav']:.1%}/{r['target_pct']:.0%}"
            for r in top_sectors if r["pct_nav"] > 0
        )
        if sec:
            lines.append(f"Sectors (actual/target): {sec}")
    if ticker:
        ctx = ticker_context(firm_state, ticker, proposed_entry_pct)
        lines.append(f"Name context: {json.dumps(ctx, default=str)}")
    return "\n".join(lines)
