"""Order sizing from plan entry fields."""
from app import plan_automation


def test_order_quantity_rounds_and_floors_to_min_one():
    plan = {
        "ticker": "TEST",
        "entry": {
            "target_size_pct_nav": 0.04,
            "entry_price_or_trigger": {"type": "limit_price", "value": 250.0},
        },
    }
    # 4% of $1M / $250 = 160 shares
    assert plan_automation.order_quantity_from_plan(
        plan, nav_usd=1_000_000, cap_by_liquidity=False,
    ) == 160


def test_order_quantity_high_price_gets_at_least_one_share():
    plan = {
        "ticker": "TEST",
        "entry": {
            "target_size_pct_nav": 0.04,
            "entry_price_or_trigger": {"type": "limit_price", "value": 900_000.0},
        },
    }
    # int(40k/900k) == 0 without min_qty; policy execution uses min 1
    assert plan_automation.order_quantity_from_plan(
        plan, nav_usd=1_000_000, cap_by_liquidity=False,
    ) == 1


def test_order_quantity_zero_pct_uses_default():
    plan = {
        "ticker": "TEST",
        "entry": {
            "target_size_pct_nav": 0,
            "entry_price_or_trigger": {"value": 100.0},
        },
    }
    assert plan_automation.order_quantity_from_plan(
        plan, nav_usd=1_000_000, cap_by_liquidity=False,
    ) == 400
