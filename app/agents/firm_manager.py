"""Portfolio Manager — policy + routing only; no trades or position decisions."""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from .. import allocation, db, llm, tools
from .. import firm_state as fs_mod

ROLE_DISCLAIMER = (
    "Portfolio Manager does NOT approve/reject plans, execute orders, open or "
    "close positions, or override Risk/HITL. It only sets firm-wide policy focus "
    "(scan bias, sector caps, freeze flags) and routes work to specialist agents."
)

SYSTEM = (
    """You are the Portfolio Manager at Horizon Capital — a **policy orchestrator**, not a trader.

You NEVER:
- approve or reject investment plans
- execute trades or simulate fills
- open, add to, or close positions
- override the Risk Officer, Plan Supervisor, or operator HITL

You ONLY:
- read the live book, allocation policy, and open work queue
- issue prioritized **tasks** (which agent should look at what, and why)
- set **scan_directives** (sector bias, deprioritize, prefer_actions, caps)
- flag **supervision_focus** tickers for the monitor/supervisor cycle

Specialist agents (Fundamental, Plan Builder, Risk, Supervisor, Execution, operator)
make all buy/sell/hold/HITL decisions. Your output is routing and policy context only.

"""
    + ROLE_DISCLAIMER
    + """

Output strict JSON:
- manager_id: str
- as_of: str
- book_summary: 2-3 sentences on cash, invested %, concentration, pending HITL
- tasks: list of {
    task_id, type, priority (high|medium|low), ticker?, sector?,
    rationale, policy_section, suggested_agent
  }
  task types: operator_hitl, review_holding, reduce_concentration,
  scan_underweight_sector, scan_diversify_portfolio, scan_add_on,
  trim_watch, freeze_new_entries, run_supervision
  Routing rules (capital-allocation §4): single-name over 8% NAV → reduce_concentration
  (trim path to Position Monitor / Fundamental); under 8 names → scan_diversify_portfolio;
  under-invested → scan_underweight_sector / Idea Scan; never add to names over cap.
- scan_directives: {
    bias_sectors: [{sector, reason, boost: 0.0-0.15}],
    deprioritize_sectors: [{sector, reason, penalty: 0.0-0.15}],
    prefer_actions: ["open_new_research"|"add_to_existing"|"watch"],
    max_new_openings: int,
    narrative: str
  }
- supervision_focus: list of {ticker, plan_id?, reason}
- reasoning_narrative: 4-8 lines
- policy_sections_cited: list
"""
)


def _underweight_sectors(firm: dict) -> list[dict]:
    out = []
    for row in firm.get("sectors", []):
        if row["pct_nav"] < row["band_low_pct"] - 0.01:
            out.append({
                "sector": row["sector"],
                "actual_pct": row["pct_nav"],
                "target_pct": row["target_pct"],
                "headroom_to_cap_pct": row["headroom_to_cap_pct"],
            })
    return sorted(out, key=lambda x: x["target_pct"] - x["actual_pct"], reverse=True)


def _concentration_tasks(firm: dict, tasks: list[dict]) -> None:
    """Single-name cap / diversification — manager routes, agents decide trim vs hold."""
    policy = firm.get("policy", {})
    max_pos = float(policy.get("max_position_pct", allocation.MAX_POSITION_PCT))
    seen: set[str] = {t.get("ticker") for t in tasks if t.get("ticker")}

    for row in allocation.concentrated_positions(firm.get("positions", [])):
        ticker = row["ticker"]
        if not ticker or ticker in seen:
            continue
        pct = row["pct_nav"]
        if row["status"] == "over_cap":
            tasks.append({
                "task_id": f"task_reduce_{ticker}",
                "type": "reduce_concentration",
                "priority": "high",
                "ticker": ticker,
                "sector": row.get("sector"),
                "rationale": (
                    f"{ticker} at {pct:.1%} NAV exceeds single-name cap {max_pos:.0%} — "
                    f"route trim review; do not add. Specialist agents decide sell/trim/HITL."
                ),
                "policy_section": "capital-allocation §4",
                "suggested_agent": "position_monitor",
            })
        else:
            tasks.append({
                "task_id": f"task_watchcap_{ticker}",
                "type": "review_holding",
                "priority": "medium",
                "ticker": ticker,
                "sector": row.get("sector"),
                "rationale": (
                    f"{ticker} at {pct:.1%} NAV approaching cap {max_pos:.0%} — "
                    f"monitor sizing; no add-on research until below warn band."
                ),
                "policy_section": "capital-allocation §4",
                "suggested_agent": "position_monitor",
            })
        seen.add(ticker)


def _diversification_scan_task(firm: dict, tasks: list[dict], freeze: bool) -> None:
    policy = firm.get("policy", {})
    lo = int((policy.get("target_position_count") or allocation.TARGET_POSITION_COUNT)[0])
    pos_n = int(firm.get("positions_count", 0))
    invested = float(firm.get("invested_pct", 0))
    if freeze or pos_n >= lo:
        return
    if any(t.get("type") == "scan_diversify_portfolio" for t in tasks):
        return
    tasks.append({
        "task_id": "task_diversify_scan",
        "type": "scan_diversify_portfolio",
        "priority": "medium",
        "ticker": None,
        "sector": None,
        "rationale": (
            f"Only {pos_n} positions (target ≥{lo}) — bias Idea Scan toward new names "
            f"for diversification; invested {invested:.1%}."
        ),
        "policy_section": "capital-allocation §4",
        "suggested_agent": "idea_generator",
    })


def _overweight_sectors(firm: dict) -> list[dict]:
    out = []
    for row in firm.get("sectors", []):
        if row["pct_nav"] > row["band_high_pct"]:
            out.append({
                "sector": row["sector"],
                "actual_pct": row["pct_nav"],
                "band_high_pct": row["band_high_pct"],
            })
    return sorted(out, key=lambda x: -x["actual_pct"])


def _mock_output(firm: dict, as_of: str) -> dict:
    from .. import hitl_sync
    hitl_sync.repair_hitl_queue()

    mid = "mgr_" + uuid.uuid4().hex[:10]
    policy = firm.get("policy", {})
    cash_pct = float(firm.get("cash_pct", 0))
    invested_pct = float(firm.get("invested_pct", 0))
    pos_n = int(firm.get("positions_count", 0))
    pending = float(firm.get("pending_hitl_deploy_pct_nav", 0))
    hitl_n = len(db.list_hitl_pending())

    tasks: list[dict] = []
    for item in db.list_hitl_pending():
        plan = db.get_plan(item["plan_id"]) or {}
        tasks.append({
            "task_id": f"task_hitl_{item['item_id']}",
            "type": "operator_hitl",
            "priority": "high",
            "ticker": (plan.get("ticker") or "?").upper(),
            "sector": None,
            "rationale": f"Operator approval required for plan {item['plan_id']}.",
            "policy_section": "firm-charter §4",
            "suggested_agent": "operator",
        })

    under = _underweight_sectors(firm)
    over = _overweight_sectors(firm)
    for sec in under[:3]:
        tasks.append({
            "task_id": f"task_scan_{sec['sector'][:8].lower()}",
            "type": "scan_underweight_sector",
            "priority": "medium",
            "ticker": None,
            "sector": sec["sector"],
            "rationale": (
                f"{sec['sector']} at {sec['actual_pct']:.1%} vs target "
                f"{sec['target_pct']:.1%} — bias Idea Scan toward this sector."
            ),
            "policy_section": "capital-allocation §3",
            "suggested_agent": "idea_generator",
        })

    for sec in over[:2]:
        tasks.append({
            "task_id": f"task_trim_{sec['sector'][:8].lower()}",
            "type": "trim_watch",
            "priority": "medium",
            "ticker": None,
            "sector": sec["sector"],
            "rationale": (
                f"{sec['sector']} above band high ({sec['actual_pct']:.1%}) — "
                f"no new names; monitor trims."
            ),
            "policy_section": "capital-allocation §7",
            "suggested_agent": "position_monitor",
        })

    _concentration_tasks(firm, tasks)

    for p in firm.get("positions", []):
        if float(p.get("unrealized_pnl_pct", 0)) <= -0.10:
            if any(t.get("ticker") == p["ticker"] for t in tasks):
                continue
            tasks.append({
                "task_id": f"task_review_{p['ticker']}",
                "type": "review_holding",
                "priority": "high",
                "ticker": p["ticker"],
                "sector": p.get("sector"),
                "rationale": (
                    f"{p['ticker']} unrealized {p['unrealized_pnl_pct']:.1%} — "
                    f"thesis review per risk-policy §2."
                ),
                "policy_section": "risk-policy §2",
                "suggested_agent": "position_monitor",
            })
        elif (
            allocation.single_name_status(float(p.get("pct_nav", 0))) == "ok"
            and 0 < float(p.get("pct_nav", 0)) < allocation.position_warn_pct()
        ):
            # Room for add-on research only when well below single-name cap
            tasks.append({
                "task_id": f"task_addon_{p['ticker']}",
                "type": "scan_add_on",
                "priority": "low",
                "ticker": p["ticker"],
                "sector": p.get("sector"),
                "rationale": (
                    f"{p['ticker']} at {p.get('pct_nav', 0):.1%} NAV — add-on research "
                    f"if sector headroom allows (below {allocation.position_warn_pct():.0%} warn)."
                ),
                "policy_section": "capital-allocation §4",
                "suggested_agent": "idea_generator",
            })

    liq = firm.get("liquidity") or allocation.liquidity_budget(
        float(firm.get("nav_usd") or 0),
        float(firm.get("cash_usd") or 0),
        pending_deploy_usd=float(firm.get("nav_usd") or 0) * pending,
        maiden_entry=True,
    )
    cash_target = float(policy.get("cash_target_pct", allocation.CASH_TARGET_PCT))
    freeze = (
        invested_pct >= policy.get("max_invested_pct", 0.92)
        or cash_pct < cash_target
        or not liq.get("can_open_new_name", True)
    )
    _diversification_scan_task(firm, tasks, freeze)
    if freeze:
        tasks.append({
            "task_id": "task_freeze_entries",
            "type": "freeze_new_entries",
            "priority": "high",
            "ticker": None,
            "sector": None,
            "rationale": (
                f"Invested {invested_pct:.1%} / cash {cash_pct:.1%} — "
                f"defer maiden entries per capital-allocation §2."
            ),
            "policy_section": "capital-allocation §2",
            "suggested_agent": "risk_officer",
        })

    bias = [
        {
            "sector": s["sector"],
            "reason": f"Underweight vs target ({s['actual_pct']:.1%})",
            "boost": 0.12,
        }
        for s in under[:5]
    ]
    deprior = [
        {
            "sector": s["sector"],
            "reason": f"Above band high ({s['actual_pct']:.1%})",
            "penalty": 0.15,
        }
        for s in over
    ]
    prefer = ["add_to_existing", "watch"]
    max_new = 2
    deploy_urgency = "normal"
    priority_tickers: list[str] = []
    bias_asset_classes: list[dict] = []
    over_cap = [
        r["ticker"] for r in allocation.concentrated_positions(
            firm.get("positions", []), include_approaching=False,
        )
    ]
    held = set(firm.get("holdings_tickers") or [])
    if invested_pct < policy.get("min_invested_pct", 0.70):
        prefer = ["open_new_research", "add_to_existing"]
        max_new = 5
        deploy_urgency = "high"
        bias_asset_classes = [
            {"asset_class": "commodity_proxy", "boost": 0.08, "reason": "diversify book"},
            {"asset_class": "rates_proxy", "boost": 0.06, "reason": "rates sleeve"},
            {"asset_class": "fx_proxy", "boost": 0.05, "reason": "fx hedge sleeve"},
            {"asset_class": "crypto", "boost": 0.06, "reason": "digital assets satellite"},
        ]
    elif pos_n < int(policy.get("min_position_count", 10)):
        prefer = ["open_new_research", "add_to_existing"]
        max_new = max(max_new, 4)
        deploy_urgency = "high"
    # Example priority names in underweight sectors (not held)
    try:
        from .. import asset_universe
        pool = asset_universe.pool_rows()
        for sec in under[:3]:
            sec_name = sec["sector"]
            for t, row in pool.items():
                if t in held:
                    continue
                if allocation.normalize_sector(row.get("sector", "")) == sec_name:
                    priority_tickers.append(t)
                if len(priority_tickers) >= 8:
                    break
            if len(priority_tickers) >= 8:
                break
    except Exception:
        pass
    if over_cap:
        prefer = [a for a in prefer if a != "add_to_existing"] or ["watch"]
        max_new = min(max_new, 1)
    if freeze:
        prefer = ["watch"]
        max_new = 0
    max_new = min(max_new, int(liq.get("max_new_maiden_entries") or 0))

    sup_focus = []
    for row in db.list_plans(status="active"):
        t = (row.get("ticker") or "").upper()
        canon = db.canonical_active_plan_for_ticker(t)
        if canon and canon["plan_id"] != row["plan_id"]:
            continue
        if any(s["ticker"].upper() == t for s in sup_focus):
            continue
        sup_focus.append({
            "ticker": row["ticker"],
            "plan_id": row["plan_id"],
            "reason": "Active plan — include in supervision cycle",
        })

    from .. import trading_posture
    posture = trading_posture.derive_posture(firm)
    scan_out = {
        "bias_sectors": bias,
        "deprioritize_sectors": deprior,
        "prefer_actions": prefer,
        "max_new_openings": max_new,
        "max_deploy_usd": liq.get("deployable_cash_usd"),
        "max_maiden_entry_pct_nav": liq.get("max_maiden_entry_pct_nav"),
        "liquidity_status": liq.get("status"),
        "deploy_urgency": deploy_urgency,
        "priority_tickers": priority_tickers[:10],
        "bias_asset_classes": bias_asset_classes,
        "narrative": (
            f"Bias scan toward {len(bias)} underweight sectors; "
            f"deprioritize {len(deprior)} overweight; "
            f"prefer {', '.join(prefer)}; "
            f"deploy_urgency={deploy_urgency}; "
            f"{len(priority_tickers)} priority names."
        ),
    }
    scan_out = trading_posture.merge_scan_directives(scan_out, posture)

    return {
        "manager_id": mid,
        "as_of": as_of,
        "trading_posture": posture,
        "agent_guidance": posture.get("agent_guidance", {}),
        "book_summary": (
            f"NAV ${firm['nav_usd']:,.0f}; cash {cash_pct:.1%}; invested "
            f"{invested_pct:.1%}; {pos_n} positions; pending HITL deploy "
            f"{pending:.1%}; {hitl_n} in operator queue."
        ),
        "tasks": tasks[:12],
        "scan_directives": scan_out,
        "supervision_focus": sup_focus[:20],
        "reasoning_narrative": (
            f"Portfolio Manager cycle {as_of}: {len(tasks)} tasks issued. "
            f"Cash {cash_pct:.1%} vs target {policy.get('cash_target_pct', 0.08):.0%}. "
            f"Invested {invested_pct:.1%} (band "
            f"{policy.get('min_invested_pct', 0.7):.0%}–"
            f"{policy.get('max_invested_pct', 0.92):.0%}). "
            + ("; freeze new entries" if freeze else "")
            + (f"; {len(db.list_hitl_pending())} HITL pending" if db.list_hitl_pending() else "")
        ),
        "policy_sections_cited": [
            "capital-allocation §1", "capital-allocation §3",
            "portfolio-manager §3",
        ],
        "firm_snapshot": {
            "cash_pct": cash_pct,
            "invested_pct": invested_pct,
            "positions_count": pos_n,
            "pending_hitl_pct": pending,
        },
    }


def format_directives_block(manager_out: Optional[dict]) -> str:
    if not manager_out:
        return ""
    from .. import trading_posture
    posture = manager_out.get("trading_posture")
    if not posture and manager_out.get("firm_snapshot"):
        pass
    lines = [
        f"PORTFOLIO MANAGER ({manager_out.get('manager_id', 'mgr')}) — policy/routing only:",
        ROLE_DISCLAIMER,
        manager_out.get("book_summary", ""),
    ]
    if posture:
        lines.append(trading_posture.format_posture_block(posture))
    elif manager_out.get("agent_guidance"):
        lines.append("Agent guidance: " + json.dumps(manager_out["agent_guidance"], indent=0)[:800])
    sd = manager_out.get("scan_directives") or {}
    if sd.get("narrative"):
        lines.append(f"Scan: {sd['narrative']}")
    if sd.get("prefer_actions"):
        lines.append(f"Prefer actions: {', '.join(sd['prefer_actions'])}")
    if sd.get("max_new_openings") is not None:
        lines.append(f"Max new openings this scan: {sd['max_new_openings']}")
    if sd.get("deploy_urgency"):
        lines.append(f"Deploy urgency: {sd['deploy_urgency']}")
    if sd.get("priority_tickers"):
        lines.append(f"Priority tickers: {', '.join(sd['priority_tickers'][:12])}")
    if sd.get("bias_asset_classes"):
        lines.append(
            "Bias asset classes: "
            + ", ".join(
                f"{r.get('asset_class')}(+{r.get('boost', 0)})"
                for r in sd["bias_asset_classes"][:6]
            )
        )
    tasks = manager_out.get("tasks") or []
    if tasks:
        lines.append("Tasks (top):")
        for t in tasks[:6]:
            tick = t.get("ticker") or t.get("sector") or "—"
            lines.append(
                f"  - [{t.get('priority')}] {t.get('type')}: {tick} — "
                f"{(t.get('rationale') or '')[:120]}"
            )
    return "\n".join(lines)


def sector_score_adjustment(sector: str, manager_out: Optional[dict]) -> float:
    """Additive adjustment to composite fit from manager scan_directives."""
    if not manager_out:
        return 0.0
    sec = allocation.normalize_sector(sector)
    sd = manager_out.get("scan_directives") or {}
    adj = 0.0
    for row in sd.get("bias_sectors") or []:
        if allocation.normalize_sector(row.get("sector", "")) == sec:
            adj += float(row.get("boost") or 0.08)
    for row in sd.get("deprioritize_sectors") or []:
        if allocation.normalize_sector(row.get("sector", "")) == sec:
            adj -= float(row.get("penalty") or 0.10)
    return max(-0.18, min(0.20, adj))


def ticker_score_adjustment(ticker: str, manager_out: Optional[dict]) -> float:
    """Per-ticker boost from manager priority list and scan tasks."""
    if not manager_out:
        return 0.0
    t = ticker.upper()
    sd = manager_out.get("scan_directives") or {}
    adj = 0.0
    if t in set(sd.get("priority_tickers") or []):
        adj += 0.12
    for task in manager_out.get("tasks") or []:
        if task.get("ticker") == t and task.get("type") in (
            "scan_underweight_sector", "scan_diversify_portfolio",
        ):
            adj += 0.08
        if task.get("type") == "freeze_new_entries":
            return -0.30
    return max(-0.25, min(0.20, adj))


def latest_snapshot(firm_state: Optional[dict] = None) -> dict:
    """Last firm_manager output from a run, or deterministic mock for UI."""
    for row in db.list_runs(40):
        run = db.get_run(row["run_id"])
        if not run:
            continue
        try:
            state = json.loads(run.get("state_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        mgr = state.get("firm_manager")
        if mgr and mgr.get("tasks") is not None:
            return mgr
    as_of = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not firm_state:
        firm_state = tools.get_firm_state(refresh_prices=False)
    return _mock_output(firm_state, as_of)


def run(
    firm_state: Optional[dict] = None,
    as_of: str = "",
) -> dict:
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    if not firm_state:
        firm_state = tools.get_firm_state(refresh_prices=False)

    policy_hits = tools.search_policy(
        query="portfolio manager capital allocation sector weights cash holdings",
        top_k=5,
    )
    portfolio_block = fs_mod.format_for_prompt(firm_state)
    pending = db.list_hitl_pending()
    open_plans = db.list_plans()

    conc = firm_state.get("concentration") or allocation.concentrated_positions(
        firm_state.get("positions", []),
    )
    user = (
        f"As of: {as_of}\n\n"
        f"LIVE BOOK:\n{portfolio_block}\n\n"
        f"Single-name concentration (cap {allocation.MAX_POSITION_PCT:.0%}, "
        f"warn {allocation.position_warn_pct():.0%}):\n"
        f"{json.dumps(conc, indent=2)[:1200] or 'none'}\n\n"
        f"Pending HITL: {len(pending)} items\n"
        f"Open plans: {json.dumps(open_plans[:15], default=str)[:2000]}\n\n"
        f"Policy retrieval:\n"
        f"{json.dumps(policy_hits.get('hits', []), indent=2)[:2000]}\n\n"
        "Issue tasks and scan_directives as strict JSON. "
        "Over 8% NAV → reduce_concentration; under 10 names → scan_diversify; "
        "you route work and set policy — you do not decide trades."
    )
    mock = _mock_output(firm_state, as_of)
    out = llm.chat_json(SYSTEM, user, mock_fallback=mock, purpose="firm_manager")
    out.setdefault("manager_id", mock["manager_id"])
    out.setdefault("as_of", as_of)
    out.setdefault("scan_directives", mock.get("scan_directives", {}))
    out.setdefault("tasks", mock.get("tasks", []))
    if not out.get("book_summary"):
        out["book_summary"] = mock["book_summary"]
    from .. import trading_posture
    posture = trading_posture.derive_posture(firm_state)
    out["trading_posture"] = posture
    out["agent_guidance"] = posture.get("agent_guidance", {})
    out["scan_directives"] = trading_posture.merge_scan_directives(
        out.get("scan_directives") or mock.get("scan_directives", {}),
        posture,
    )
    return out
