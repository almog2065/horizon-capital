"""Capital allocation targets (capital-allocation policy §1–§4)."""
from __future__ import annotations

from typing import Any, Optional

# Hard limits (also enforced in tools.simulate_order)
CASH_FLOOR_PCT = 0.05
CASH_TARGET_PCT = 0.08
CASH_CEILING_PCT = 0.20
MAX_SECTOR_PCT = 0.25
MAX_POSITION_PCT = 0.08
# Warn when a name reaches this fraction of the hard cap (manager routes review)
POSITION_WARN_RATIO = 0.875  # 7% NAV when cap is 8%
PER_ORDER_MAX_PCT = 0.05
MAIDEN_ENTRY_PCT = 0.03
DEFAULT_ENTRY_PCT = 0.04

TARGET_INVESTED_PCT = 0.85
MIN_INVESTED_PCT = 0.70
MAX_INVESTED_PCT = 0.92
TARGET_POSITION_COUNT = (10, 16)

# Strategic sector targets (policy §3) — target weight and tolerance band
SECTOR_TARGETS: dict[str, dict[str, float]] = {
    "Information Technology": {"target": 0.28, "band": 0.05},
    "Health Care": {"target": 0.18, "band": 0.05},
    "Financials": {"target": 0.12, "band": 0.05},
    "Consumer Discretionary": {"target": 0.10, "band": 0.04},
    "Industrials": {"target": 0.10, "band": 0.04},
    "Communication Services": {"target": 0.08, "band": 0.03},
    "Energy": {"target": 0.05, "band": 0.03},
    "Materials": {"target": 0.04, "band": 0.02},
    "Real Estate": {"target": 0.03, "band": 0.02},
    "Utilities": {"target": 0.02, "band": 0.02},
    # Satellite sleeves (policy §8 multi-asset data)
    "Digital Assets": {"target": 0.03, "band": 0.02},
    "Commodities": {"target": 0.04, "band": 0.02},
    "Rates": {"target": 0.03, "band": 0.02},
    "Currencies": {"target": 0.02, "band": 0.02},
}

# Aggregate cap for crypto sleeve (policy §8)
MAX_DIGITAL_ASSETS_PCT = 0.10


def liquidity_budget(
    nav: float,
    cash_usd: float,
    *,
    pending_deploy_usd: float = 0.0,
    maiden_entry: bool = True,
) -> dict[str, Any]:
    """Deployable cash after mandatory reserve (capital-allocation §1).

    New names must preserve the **cash target** (8% NAV). Add-ons may draw
    only down to the **hard floor** (5% NAV) when already held.
    """
    if nav <= 0:
        return {
            "deployable_cash_usd": 0.0,
            "deployable_pct_nav": 0.0,
            "cash_reserve_usd": 0.0,
            "reserve_pct": CASH_TARGET_PCT,
            "max_entry_pct_nav": 0.0,
            "max_maiden_entry_pct_nav": 0.0,
            "max_new_maiden_entries": 0,
            "can_open_new_name": False,
            "status": "no_nav",
        }

    reserve_pct = CASH_TARGET_PCT if maiden_entry else CASH_FLOOR_PCT
    reserve_usd = nav * reserve_pct
    pro_forma_cash = cash_usd - pending_deploy_usd
    deployable = max(0.0, pro_forma_cash - reserve_usd)
    cash_pct = cash_usd / nav

    max_entry_pct = min(
        PER_ORDER_MAX_PCT,
        deployable / nav if nav else 0.0,
    )
    max_maiden_pct = min(MAIDEN_ENTRY_PCT, max_entry_pct)
    slots = int(deployable / (nav * MAIDEN_ENTRY_PCT)) if nav and MAIDEN_ENTRY_PCT else 0

    if cash_pct < CASH_FLOOR_PCT - 1e-6:
        status = "breach_floor"
    elif cash_pct < CASH_TARGET_PCT - 0.005:
        status = "below_cash_target"
    elif cash_pct > CASH_CEILING_PCT:
        status = "excess_cash"
    else:
        status = "healthy"

    return {
        "cash_usd": round(cash_usd, 2),
        "cash_pct": round(cash_pct, 4),
        "cash_floor_usd": round(nav * CASH_FLOOR_PCT, 2),
        "cash_target_usd": round(nav * CASH_TARGET_PCT, 2),
        "cash_reserve_usd": round(reserve_usd, 2),
        "reserve_pct": reserve_pct,
        "pending_deploy_usd": round(pending_deploy_usd, 2),
        "pro_forma_cash_usd": round(pro_forma_cash, 2),
        "deployable_cash_usd": round(deployable, 2),
        "deployable_pct_nav": round(deployable / nav, 4),
        "max_entry_pct_nav": round(max_entry_pct, 4),
        "max_maiden_entry_pct_nav": round(max_maiden_pct, 4),
        "max_new_maiden_entries": max(0, slots),
        "can_open_new_name": deployable >= nav * MAIDEN_ENTRY_PCT - 1e-6,
        "status": status,
    }


def cap_entry_pct_for_liquidity(
    entry_pct: float,
    liquidity: dict[str, Any],
    *,
    maiden: bool = True,
) -> float:
    """Clip proposed entry size to deployable cash and per-order cap."""
    cap = liquidity.get("max_maiden_entry_pct_nav" if maiden else "max_entry_pct_nav")
    if cap is None:
        return entry_pct
    return max(0.0, min(float(entry_pct), float(cap)))


def normalize_sector(sector: str) -> str:
    s = (sector or "Unknown").strip()
    if s in SECTOR_TARGETS:
        return s
    aliases = {
        "Technology": "Information Technology",
        "IT": "Information Technology",
        "Healthcare": "Health Care",
        "Consumer Cyclical": "Consumer Discretionary",
        "Telecommunication": "Communication Services",
        "Telecommunications": "Communication Services",
    }
    return aliases.get(s, s if s != "Unknown" else "Other")


def sector_band(sector: str) -> dict[str, float]:
    sec = normalize_sector(sector)
    if sec in SECTOR_TARGETS:
        t = SECTOR_TARGETS[sec]
        return {
            "target_pct": t["target"],
            "band_pct": t["band"],
            "band_low_pct": max(0.0, t["target"] - t["band"]),
            "band_high_pct": min(MAX_SECTOR_PCT, t["target"] + t["band"]),
        }
    return {
        "target_pct": 0.0,
        "band_pct": 0.0,
        "band_low_pct": 0.0,
        "band_high_pct": MAX_SECTOR_PCT,
    }


def sector_exposure_pct(
    sector_allocations: dict[str, float],
    sector: str,
    nav: float,
) -> float:
    if nav <= 0:
        return 0.0
    sec = normalize_sector(sector)
    usd = sector_allocations.get(sec, 0.0)
    for k, v in sector_allocations.items():
        if normalize_sector(k) == sec:
            usd += v if k != sec else 0.0
    return usd / nav


def analyze_sector_headroom(
    sector: str,
    current_sector_pct: float,
    proposed_add_pct: float,
) -> dict[str, Any]:
    """Headroom for a proposed add in the ticker's sector."""
    band = sector_band(sector)
    pro_forma = current_sector_pct + proposed_add_pct
    hard_headroom = max(0.0, MAX_SECTOR_PCT - current_sector_pct)
    soft_headroom = max(0.0, band["band_high_pct"] - current_sector_pct)
    return {
        "sector": normalize_sector(sector),
        "current_pct_nav": round(current_sector_pct, 4),
        "proposed_add_pct_nav": round(proposed_add_pct, 4),
        "pro_forma_pct_nav": round(pro_forma, 4),
        "target_pct_nav": band["target_pct"],
        "band_high_pct": band["band_high_pct"],
        "hard_cap_pct": MAX_SECTOR_PCT,
        "headroom_to_hard_cap_pct": round(hard_headroom, 4),
        "headroom_to_band_high_pct": round(soft_headroom, 4),
        "within_hard_cap": pro_forma <= MAX_SECTOR_PCT + 1e-6,
        "within_band": pro_forma <= band["band_high_pct"] + 1e-6,
    }


def position_warn_pct() -> float:
    return round(MAX_POSITION_PCT * POSITION_WARN_RATIO, 4)


def single_name_status(pct_nav: float) -> str:
    """ok | approaching_cap | over_cap — per capital-allocation §4 / risk-policy §2."""
    pct = float(pct_nav or 0)
    if pct >= MAX_POSITION_PCT - 1e-6:
        return "over_cap"
    if pct >= position_warn_pct() - 1e-6:
        return "approaching_cap"
    return "ok"


def concentrated_positions(
    positions: list[dict],
    *,
    include_approaching: bool = True,
) -> list[dict]:
    """Holdings at or above single-name concentration thresholds."""
    out: list[dict] = []
    for p in positions or []:
        pct = float(p.get("pct_nav") or 0)
        status = single_name_status(pct)
        if status == "over_cap" or (include_approaching and status == "approaching_cap"):
            out.append({
                "ticker": p.get("ticker"),
                "sector": p.get("sector"),
                "pct_nav": pct,
                "status": status,
                "max_position_pct": MAX_POSITION_PCT,
                "warn_pct": position_warn_pct(),
            })
    return sorted(out, key=lambda x: -x["pct_nav"])


def portfolio_decision_hints(
    cash_pct: float,
    invested_pct: float,
    positions_count: int,
    sector_rows: list[dict],
    pending_deploy_pct: float,
    positions: Optional[list[dict]] = None,
) -> list[str]:
    hints: list[str] = []
    if cash_pct < CASH_FLOOR_PCT + 0.01:
        hints.append("Cash near hard floor — avoid new entries until liquidity improves.")
    elif cash_pct > CASH_CEILING_PCT:
        hints.append("Cash above 20% ceiling — bias toward deployment on quality setups.")
    elif cash_pct < CASH_TARGET_PCT:
        hints.append(
            f"Cash {cash_pct:.1%} below {CASH_TARGET_PCT:.0%} operating target — "
            f"no new maiden entries until reserve restored; add-ons only within deployable cash."
        )
    elif cash_pct > CASH_TARGET_PCT:
        hints.append(f"Cash {cash_pct:.1%} above {CASH_TARGET_PCT:.0%} target — room to deploy.")
    if invested_pct < MIN_INVESTED_PCT:
        hints.append("Book under-invested (<70% NAV) — favor new names in underweight sectors.")
    elif invested_pct > MAX_INVESTED_PCT:
        hints.append("Book over-invested (>92% NAV) — new entries only as small add-ons.")
    lo, hi = TARGET_POSITION_COUNT
    if positions_count < lo:
        hints.append(
            f"Only {positions_count} positions (target {lo}–{hi}) — "
            f"bias Idea Scan toward new names for diversification."
        )
    elif positions_count > hi:
        hints.append(f"{positions_count} positions (target {lo}–{hi}) — prefer add-ons over new names.")
    if positions:
        hints.extend(portfolio_concentration_hints(positions))
    if pending_deploy_pct >= 0.02:
        hints.append(
            f"Pending HITL deployment ~{pending_deploy_pct:.1%} NAV — include in pro-forma liquidity."
        )
    for row in sector_rows:
        if row.get("pct_nav", 0) >= MAX_SECTOR_PCT - 0.02:
            hints.append(f"{row['sector']} at {row['pct_nav']:.1%} NAV — near sector hard cap.")
        elif row.get("pct_nav", 0) > row.get("band_high_pct", 1):
            hints.append(f"{row['sector']} above strategic band high — avoid adds in sector.")
    return hints[:10]


def portfolio_concentration_hints(positions: list[dict]) -> list[str]:
    hints: list[str] = []
    for row in concentrated_positions(positions, include_approaching=False):
        hints.append(
            f"{row['ticker']} {row['pct_nav']:.1%} NAV — exceeds {MAX_POSITION_PCT:.0%} "
            f"single-name cap; route trim review (not new adds)."
        )
    for row in concentrated_positions(positions, include_approaching=True):
        if row["status"] != "approaching_cap":
            continue
        hints.append(
            f"{row['ticker']} {row['pct_nav']:.1%} NAV — approaching "
            f"{MAX_POSITION_PCT:.0%} cap (warn {position_warn_pct():.0%})."
        )
    return hints[:6]
