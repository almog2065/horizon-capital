"""Trading guardrail checks used during eval replay (mirrors policy caps)."""
from __future__ import annotations

from typing import Any

# Keep in sync with app.allocation hard limits used in simulate_order.
PER_ORDER_MAX_PCT = 0.05
MAX_POSITION_PCT = 0.08


def check_trade(
    ev: dict,
    *,
    nav: float,
    holdings: dict[str, dict[str, float]],
) -> tuple[bool, list[str]]:
    """
    Return (passed, violations) for a proposed trade event.

    Honors ``expect_guardrail_block`` in fixtures (deterministic breach).
    """
    if ev.get("expect_guardrail_block"):
        return False, list(ev.get("violations") or ["fixture: expected guardrail block"])

    ticker = ev.get("ticker", "")
    action = ev.get("action", "")
    qty = float(ev.get("qty") or 0)
    px = float(ev.get("price") or 0)
    if qty <= 0 or px <= 0:
        return False, ["invalid quantity or price"]

    notional = qty * px
    if nav > 0 and notional > nav * PER_ORDER_MAX_PCT * 1.001:
        return False, [
            f"order notional ${notional:,.0f} exceeds per-order cap "
            f"{PER_ORDER_MAX_PCT:.0%} NAV (${nav * PER_ORDER_MAX_PCT:,.0f})",
        ]

    if action == "buy" and nav > 0:
        pos = holdings.get(ticker, {"qty": 0.0, "avg_cost": px})
        new_qty = pos.get("qty", 0.0) + qty
        post_mv = new_qty * px
        if post_mv > nav * MAX_POSITION_PCT * 1.001:
            return False, [
                f"post-trade {ticker} weight exceeds {MAX_POSITION_PCT:.0%} NAV cap",
            ]

    if ev.get("require_citations", True) and not (ev.get("citations") or []):
        return False, ["LLM output missing required citations"]

    return True, []
