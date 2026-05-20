"""Spawn specialist agent runs from Portfolio Manager routing (no trades)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Optional

from pathlib import Path

from . import allocation, config, db, firm_state as fs_mod, traces

ROOT = Path(__file__).resolve().parent.parent
_CANDIDATES_PATH = ROOT / "data" / "candidates.json"


def _last_run_age_sec(trigger_type: str) -> Optional[float]:
    with db.conn() as c:
        row = c.execute(
            "SELECT created_at FROM runs WHERE trigger_type=? "
            "ORDER BY created_at DESC LIMIT 1",
            (trigger_type,),
        ).fetchone()
    if not row:
        return None
    return max(0.0, time.time() - float(row["created_at"]))


def _load_candidate_pool() -> list[dict]:
    try:
        data = json.loads(_CANDIDATES_PATH.read_text(encoding="utf-8"))
        return list(data.get("candidate_pool") or [])
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _pick_new_candidate_for_sector(
    sector: str,
    firm_state: dict,
    *,
    exclude: Optional[set[str]] = None,
) -> Optional[str]:
    """New-name candidate in sector not currently held."""
    sec = allocation.normalize_sector(sector)
    held = {
        (p.get("ticker") or "").upper()
        for p in firm_state.get("positions", [])
    }
    exclude = exclude or set()
    pool = _load_candidate_pool()
    # Prefer dossier-backed names in sector
    in_sector = [
        c for c in pool
        if allocation.normalize_sector(c.get("sector", "")) == sec
    ]
    in_sector.sort(key=lambda c: (not c.get("has_dossier"), c.get("ticker", "")))
    for c in in_sector:
        t = (c.get("ticker") or "").upper()
        if t and t not in held and t not in exclude:
            return t
    return None


def _spawn_sector_discovery(
    sector: str,
    task: dict,
    firm_state: dict,
    manager_id: str,
    as_of: str,
    parent_run_id: str,
    seen_tickers: set[str],
) -> Optional[dict]:
    """Fallback when Idea Scan is on cooldown — route one new name in sector."""
    from . import graph

    ticker = _pick_new_candidate_for_sector(sector, firm_state, exclude=seen_tickers)
    if not ticker:
        return None
    if config.BLOCK_PIPELINE_IF_PENDING_HITL and db.pending_hitl_for_ticker(ticker):
        return None
    if config.BLOCK_DUPLICATE_PIPELINE and db.active_plan_for_ticker(ticker):
        return None
    disc_task = {
        **task,
        "ticker": ticker,
        "rationale": (
            f"{sector} underweight — discovery on {ticker} "
            f"(Idea Scan cooldown). {task.get('rationale', '')[:160]}"
        ),
    }
    news = _balance_review_event(
        ticker, disc_task, manager_id, as_of, event_kind="sector_discovery",
    )
    child_run_id = graph.spawn_news_run_background(news, as_of=as_of)
    seen_tickers.add(ticker)
    return {
        "type": task.get("type", "scan_underweight_sector"),
        "ticker": ticker,
        "sector": sector,
        "run_id": child_run_id,
        "final_status": "running",
    }


def _positions_in_sector(firm_state: dict, sector: str) -> list[dict]:
    sec = allocation.normalize_sector(sector)
    return [
        p for p in firm_state.get("positions", [])
        if allocation.normalize_sector(p.get("sector", "Unknown")) == sec
    ]


def _balance_review_event(
    ticker: str,
    task: dict,
    manager_id: str,
    as_of: str,
    *,
    event_kind: str = "review",
) -> dict:
    """Synthetic news event — routes to Fundamental → Plan → Risk (they decide)."""
    return {
        "id": f"mgr_{manager_id}_{ticker.lower()}_{int(time.time())}",
        "headline": (
            f"{ticker}: Portfolio Manager — {event_kind} "
            f"({task.get('type', 'balance')})"
        ),
        "body": (
            f"Horizon Capital Portfolio Manager {manager_id} at {as_of} routed "
            f"{event_kind} on {ticker} to specialist agents (manager does not trade). "
            f"Policy: {task.get('policy_section', 'capital-allocation')}. "
            f"Rationale: {task.get('rationale', '')[:500]} "
            f"Fundamental / Plan Builder / Risk / Supervisor must evaluate thesis, "
            f"plan eligibility, and execution — manager output is policy context only."
        ),
        "tickers": [ticker.upper()],
        "source": "firm_manager",
        "manager_task_id": task.get("task_id"),
        "manager_task_type": task.get("type"),
        "published_at": as_of,
    }


def _sector_rebalance_event(
    sector: str,
    task: dict,
    manager_id: str,
    as_of: str,
    ticker: str,
) -> dict:
    return {
        "id": f"mgr_{manager_id}_{ticker.lower()}_sec_{int(time.time())}",
        "headline": (
            f"{ticker}: sector rebalance — {sector} "
            f"({task.get('type')})"
        ),
        "body": (
            f"Portfolio Manager {manager_id}: {task.get('rationale', '')[:400]} "
            f"Review {ticker} in context of {sector} allocation vs firm targets."
        ),
        "tickers": [ticker.upper()],
        "source": "firm_manager",
        "manager_task_id": task.get("task_id"),
        "published_at": as_of,
    }


def execute_balance_actions(
    manager_out: dict,
    firm_state: dict,
    as_of: str,
    parent_run_id: str,
    *,
    allow_scan: bool = True,
    spawn_pipeline: bool = True,
    max_triggers: Optional[int] = None,
    force_scan: bool = False,
) -> dict[str, Any]:
    """
    Spawn specialist pipelines from manager routing tasks (no direct trading).
    Returns orchestration report with spawned run ids.
    """
    from . import graph

    if not config.FIRM_MANAGER_AUTO_TRIGGER:
        return {
            "executed": False,
            "reason": "FIRM_MANAGER_AUTO_TRIGGER=off",
            "spawned_run_ids": [],
            "actions": [],
        }

    cap = max_triggers if max_triggers is not None else config.FIRM_MANAGER_MAX_TRIGGERS_PER_CYCLE
    spawned: list[str] = []
    actions: list[dict] = []
    skipped: list[dict] = []
    manager_id = manager_out.get("manager_id", "mgr")
    policy = firm_state.get("policy", {})
    invested = float(firm_state.get("invested_pct", 0))
    cash_pct = float(firm_state.get("cash_pct", 0))
    freeze = any(
        t.get("type") == "freeze_new_entries"
        for t in manager_out.get("tasks") or []
    )

    def _room() -> bool:
        return len(spawned) < cap

    def _record(action: dict) -> None:
        actions.append(action)

    def _ticker_pct_nav(ticker: str) -> float:
        for p in firm_state.get("positions", []):
            if (p.get("ticker") or "").upper() == ticker.upper():
                return float(p.get("pct_nav") or 0)
        return 0.0

    pos_n = int(firm_state.get("positions_count", 0))
    min_names = int(policy.get("min_position_count", 10))
    need_deploy = (
        invested < float(policy.get("min_invested_pct", 0.70))
        or cash_pct > float(policy.get("cash_ceiling_pct", 0.20))
    )
    need_diversify = pos_n < min_names
    manager_wants_scan = any(
        t.get("type") in ("scan_diversify_portfolio", "scan_underweight_sector")
        for t in manager_out.get("tasks") or []
    )
    scan_spawned = False
    # Idea Scan: deploy cash, diversify book, or explicit manager scan tasks
    if allow_scan and (need_deploy or need_diversify or manager_wants_scan) and not freeze and _room():
        age = _last_run_age_sec("idea_scan")
        cooldown = max(60, int(config.FIRM_MANAGER_SCAN_COOLDOWN_SEC))
        bypass_cooldown = force_scan and config.FIRM_MANAGER_BALANCE_FORCE_SCAN
        if age is not None and age < cooldown and not bypass_cooldown:
            skipped.append({
                "type": "idea_scan",
                "reason": f"cooldown ({int(age)}s < {cooldown}s) — using sector fallbacks",
            })
        else:
            try:
                top_k = max(1, int(config.FIRM_MANAGER_SCAN_TOP_K))
                spawn_ds = spawn_pipeline and config.AUTO_PLAN_SPAWN_PIPELINE
                deploy = fs_mod.deployment_needs(firm_state)
                scan_only_new = not deploy["active"]
                begun = graph.begin_idea_scan(
                    top_k=top_k,
                    as_of=as_of,
                    spawn_downstream=spawn_ds,
                    only_new=scan_only_new,
                )
                scan_id = begun.scan_run_id
                scan_spawned = True
                spawned.append(scan_id)

                def _finish_scan() -> None:
                    try:
                        graph.execute_idea_scan(
                            scan_id,
                            top_k=top_k,
                            spawn_downstream=spawn_ds,
                            only_new=scan_only_new,
                        )
                    except Exception as err:
                        print(f"[firm_orchestration] idea_scan {scan_id} failed: {err}")

                threading.Thread(target=_finish_scan, daemon=True).start()
                _record({
                    "type": "idea_scan",
                    "run_id": scan_id,
                    "downstream": [],
                    "reason": (
                        "Idea Scan started in background (deploy / diversify / "
                        "manager directive)"
                        + (
                            f"; deploy_mode (only_new={scan_only_new})"
                            if deploy["active"] else ""
                        )
                    ),
                })
                traces.record("manager_trigger_scan", {
                    "parent_run_id": parent_run_id,
                    "scan_run_id": scan_id,
                })
            except Exception as e:
                skipped.append({"type": "idea_scan", "reason": str(e)[:200]})

    # Priority-ordered executable tasks
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    tasks = sorted(
        manager_out.get("tasks") or [],
        key=lambda t: priority_rank.get(t.get("priority", "low"), 9),
    )
    seen_tickers: set[str] = set()

    for task in tasks:
        if not _room():
            skipped.append({
                "type": task.get("type"),
                "ticker": task.get("ticker"),
                "reason": "max_triggers reached",
            })
            continue

        ttype = task.get("type", "")
        ticker = (task.get("ticker") or "").upper().strip()
        sector = task.get("sector")

        if ttype == "operator_hitl":
            skipped.append({
                "type": ttype,
                "ticker": ticker or None,
                "reason": "requires human operator",
            })
            continue

        if ttype == "freeze_new_entries":
            continue

        if ttype in ("review_holding", "reduce_concentration", "scan_add_on") and ticker:
            if ticker in seen_tickers:
                continue
            if ttype == "scan_add_on":
                status = allocation.single_name_status(_ticker_pct_nav(ticker))
                if status != "ok":
                    skipped.append({
                        "type": ttype,
                        "ticker": ticker,
                        "reason": (
                            f"single-name {status} "
                            f"({_ticker_pct_nav(ticker):.1%} vs "
                            f"{policy.get('max_position_pct', 0.08):.0%} cap)"
                        ),
                    })
                    continue
            if config.BLOCK_PIPELINE_IF_PENDING_HITL and db.pending_hitl_for_ticker(ticker):
                skipped.append({
                    "type": ttype, "ticker": ticker,
                    "reason": "pending HITL for ticker",
                })
                continue
            if config.BLOCK_DUPLICATE_PIPELINE and db.active_plan_for_ticker(ticker):
                skipped.append({
                    "type": ttype, "ticker": ticker,
                    "reason": "open plan exists",
                })
                continue
            try:
                if ttype == "scan_add_on":
                    kind = "add_on_review"
                elif ttype == "reduce_concentration":
                    kind = "concentration_trim_review"
                else:
                    kind = "thesis_review"
                news = _balance_review_event(
                    ticker, task, manager_id, as_of, event_kind=kind,
                )
                child_run_id = graph.spawn_news_run_background(news, as_of=as_of)
                spawned.append(child_run_id)
                seen_tickers.add(ticker)
                _record({
                    "type": ttype,
                    "ticker": ticker,
                    "run_id": child_run_id,
                    "final_status": "running",
                })
            except Exception as e:
                skipped.append({
                    "type": ttype, "ticker": ticker, "reason": str(e)[:200],
                })
            continue

        if ttype in ("scan_diversify_portfolio", "scan_underweight_sector"):
            if scan_spawned:
                skipped.append({
                    "type": ttype,
                    "sector": sector,
                    "reason": "covered by Idea Scan this cycle",
                })
                continue
            if not sector and ttype == "scan_diversify_portfolio":
                # Pick first underweight sector from manager tasks or book
                under = [
                    t for t in (manager_out.get("tasks") or [])
                    if t.get("type") == "scan_underweight_sector" and t.get("sector")
                ]
                sector = (under[0].get("sector") if under else None) or (
                    (firm_state.get("sectors") or [{}])[0].get("sector")
                )
            if sector and _room():
                action = _spawn_sector_discovery(
                    sector, task, firm_state, manager_id, as_of,
                    parent_run_id, seen_tickers,
                )
                if action:
                    spawned.append(action["run_id"])
                    _record(action)
                else:
                    skipped.append({
                        "type": ttype,
                        "sector": sector,
                        "reason": "no eligible candidate or pipeline blocked",
                    })
            else:
                skipped.append({
                    "type": ttype,
                    "sector": sector,
                    "reason": "no sector / cap reached",
                })
            continue

        if ttype == "trim_watch" and ticker and not sector:
            if ticker in seen_tickers:
                continue
            try:
                news = _balance_review_event(
                    ticker, task, manager_id, as_of, event_kind="trim_review",
                )
                child_run_id = graph.spawn_news_run_background(news, as_of=as_of)
                spawned.append(child_run_id)
                seen_tickers.add(ticker)
                _record({
                    "type": ttype,
                    "ticker": ticker,
                    "run_id": child_run_id,
                })
            except Exception as e:
                skipped.append({
                    "type": ttype, "ticker": ticker, "reason": str(e)[:200],
                })
            continue

        if ttype == "trim_watch" and sector:
            positions = _positions_in_sector(firm_state, sector)
            for pos in positions[:2]:
                if not _room():
                    break
                t = pos["ticker"]
                if t in seen_tickers:
                    continue
                try:
                    trim_task = {
                        **task,
                        "rationale": (
                            f"{sector} overweight — review {t} for trim / "
                            f"rebalance per policy. {task.get('rationale', '')[:200]}"
                        ),
                    }
                    news = _balance_review_event(
                        t, trim_task, manager_id, as_of, event_kind="trim_review",
                    )
                    child_run_id = graph.spawn_news_run_background(news, as_of=as_of)
                    spawned.append(child_run_id)
                    seen_tickers.add(t)
                    _record({
                        "type": "trim_watch",
                        "ticker": t,
                        "sector": sector,
                        "run_id": child_run_id,
                    })
                except Exception as e:
                    skipped.append({
                        "type": "trim_watch",
                        "ticker": t,
                        "reason": str(e)[:200],
                    })
            continue

    return {
        "executed": True,
        "scan_spawned": scan_spawned,
        "manager_id": manager_id,
        "parent_run_id": parent_run_id,
        "spawned_run_ids": spawned,
        "actions": actions,
        "skipped": skipped,
        "cap": cap,
    }


def _save_balance_run(
    run_id: str,
    state: dict,
    as_of: str,
    status: str,
) -> None:
    db.save_run(
        run_id, "firm_balance", state["trigger_meta"], as_of, status, state,
    )


def begin_balance_cycle(
    as_of: Optional[str] = None,
    *,
    trigger_supervision: bool = True,
    spawn_pipeline: bool = True,
    force_scan: bool = False,
    cadence_phase: Optional[str] = None,
    auto_execute: Optional[bool] = None,
) -> str:
    """Create a firm_balance run row and return run_id (work continues in background)."""
    as_of = as_of or time.strftime("%Y-%m-%dT%H:%M:%S")
    run_id = "bal_" + uuid.uuid4().hex[:12]
    book = fs_mod.build_firm_state(refresh_prices=False)
    state = {
        "run_id": run_id,
        "trigger_type": "firm_balance",
        "trigger_meta": {
            "trigger_supervision": trigger_supervision,
            "spawn_pipeline": spawn_pipeline,
            "force_scan": force_scan,
            "cadence_phase": cadence_phase,
            "auto_execute": auto_execute,
        },
        "as_of": as_of,
        "firm_state": book,
        "firm_manager": None,
        "manager_orchestration": None,
        "supervision_run_id": None,
        "spawned_run_ids": [],
        "final_status": "running",
        "errors": [],
    }
    _save_balance_run(run_id, state, as_of, "running")
    traces.set_context(run_id=run_id)
    traces.record("run_start", {"trigger_type": "firm_balance"})
    return run_id


def execute_balance_cycle(
    run_id: str,
    *,
    trigger_supervision: Optional[bool] = None,
    spawn_pipeline: Optional[bool] = None,
) -> dict[str, Any]:
    """
    Run Manager → orchestration → optional supervision with checkpoint saves
    so the UI pipeline advances while work is in progress.
    """
    from . import graph
    from .agents import firm_manager

    row = db.get_run(run_id)
    if not row:
        raise ValueError(f"balance run not found: {run_id}")
    state = json.loads(row["state_json"])
    as_of = state.get("as_of") or time.strftime("%Y-%m-%dT%H:%M:%S")
    meta = state.get("trigger_meta") or {}
    if trigger_supervision is None:
        trigger_supervision = bool(meta.get("trigger_supervision", True))
    if spawn_pipeline is None:
        spawn_pipeline = bool(meta.get("spawn_pipeline", True))
    book = state.get("firm_state") or fs_mod.build_firm_state(refresh_prices=False)
    state["firm_state"] = book
    traces.set_context(run_id=run_id)

    try:
        if not state.get("firm_manager"):
            mgr_out, _, _ = graph._run_agent(
                run_id, "firm_manager", firm_manager.run, book, as_of,
            )
            state["firm_manager"] = mgr_out
            _save_balance_run(run_id, state, as_of, "running")

        mgr_out = state["firm_manager"]
        if not state.get("manager_orchestration"):
            force_scan = bool(meta.get("force_scan", False))
            orch = execute_balance_actions(
                mgr_out, book, as_of, run_id,
                spawn_pipeline=spawn_pipeline,
                force_scan=force_scan,
            )
            state["manager_orchestration"] = orch
            state["spawned_run_ids"] = list(orch.get("spawned_run_ids") or [])
            _save_balance_run(run_id, state, as_of, "running")

        if (
            trigger_supervision
            and config.AUTO_PLAN_SUPERVISION
            and not state.get("supervision_run_id")
        ):
            auto_exec = meta.get("auto_execute")
            if auto_exec is None:
                auto_exec = config.AUTO_PLAN_EXECUTE
            sup = graph.start_plan_supervision(
                as_of=as_of,
                spawn_pipeline=spawn_pipeline,
                auto_execute=bool(auto_exec),
                manager_out=mgr_out,
                skip_orchestration=True,
            )
            state["supervision_run_id"] = sup.run_id
            state["spawned_run_ids"] = list(state.get("spawned_run_ids") or [])
            state["spawned_run_ids"].extend(sup.state.get("spawned_run_ids") or [])
            _save_balance_run(run_id, state, as_of, "running")

        state["final_status"] = "completed_firm_balance"
        _save_balance_run(run_id, state, as_of, "completed")
        traces.record("run_end", {
            "final_status": state["final_status"],
            "spawned": len(state.get("spawned_run_ids") or []),
        })
        return state
    except Exception as e:
        state["errors"].append(str(e))
        state["final_status"] = "error"
        _save_balance_run(run_id, state, as_of, "error")
        traces.record("run_end", {"final_status": "error", "error": str(e)})
        raise


def recover_stale_balance_runs(max_age_sec: int = 120) -> list[str]:
    """Resume or close firm_balance runs stuck without orchestration."""
    resumed: list[str] = []
    now = time.time()
    for row in db.list_runs(30):
        if row.get("trigger_type") != "firm_balance" or row.get("status") != "running":
            continue
        age = now - float(row.get("created_at") or now)
        if age < max_age_sec:
            continue
        run_id = row["run_id"]
        try:
            print(f"[firm_balance] recovering stale run {run_id} (age {int(age)}s)")
            execute_balance_cycle(run_id)
            resumed.append(run_id)
        except Exception as e:
            print(f"[firm_balance] recovery failed {run_id}: {e}")
            try:
                st = json.loads(db.get_run(run_id)["state_json"])
                st["errors"].append(str(e))
                st["final_status"] = "error"
                _save_balance_run(run_id, st, st.get("as_of", ""), "error")
            except Exception:
                pass
    return resumed


def run_balance_background(
    run_id: str,
    *,
    trigger_supervision: bool = True,
    spawn_pipeline: bool = True,
) -> None:
    try:
        print(f"[firm_balance] background start {run_id}")
        execute_balance_cycle(
            run_id,
            trigger_supervision=trigger_supervision,
            spawn_pipeline=spawn_pipeline,
        )
        print(f"[firm_balance] background done {run_id}")
    except Exception as e:
        print(f"[firm_balance] background failed {run_id}: {e}")


def run_balance_cycle(
    as_of: Optional[str] = None,
    *,
    trigger_supervision: bool = True,
    spawn_pipeline: bool = True,
) -> dict[str, Any]:
    """Synchronous balance cycle (scheduler / rerun)."""
    run_id = begin_balance_cycle(
        as_of=as_of,
        trigger_supervision=trigger_supervision,
        spawn_pipeline=spawn_pipeline,
    )
    return execute_balance_cycle(
        run_id,
        trigger_supervision=trigger_supervision,
        spawn_pipeline=spawn_pipeline,
    )
