"""FastAPI lifespan management — split out of main.py for clarity.

The lifespan does three things:
1. Boot persistent state (sqlite, RAG corpora, HITL queue, etc.)
2. Optionally start background tasks (scheduler) when running inside the
   API process. In a multi-container deployment those tasks live in
   the dedicated `worker` container instead.
3. Cancel & await tasks on shutdown.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .logging import get_logger, setup_logging
from .settings import get_settings

log = get_logger("horizon.lifecycle")


async def _bootstrap() -> None:
    """One-shot boot work shared by API and worker entry points."""
    from .. import (
        bootstrap_data,
        db,
        firm_bootstrap,
        firm_orchestration,
        hitl_sync,
        ops_db,
        rag_bootstrap,
        traces,
        trade_history,
    )

    cfg = get_settings()
    log.info(
        "bootstrap-start", extra={
            "event": "bootstrap",
            "firm_db": str(cfg.FIRM_DB),
            "vector_db": str(cfg.VECTOR_DB),
            "ops_db": str(cfg.OPS_DB),
        },
    )

    db.init_db()
    traces.init_db()
    ops_db.init_db()
    boot_ops = bootstrap_data.ensure_runtime_bootstrap()
    if boot_ops.get("migrated", {}).get("alerts") or boot_ops.get("migrated", {}).get("daily_plans"):
        log.info("ops-db-migrated: %s", boot_ops["migrated"])
    if boot_ops.get("dossiers", {}).get("seeded"):
        log.info("ops-dossiers-seeded: %s", boot_ops["dossiers"])
    traces.configure_langsmith()

    boot = firm_bootstrap.ensure_balanced_book()
    if boot.get("seeded"):
        log.info("firm-bootstrap: %s", boot)

    seed = trade_history.ensure_trade_history_seeded()
    if seed.get("after", 0):
        log.info("trade-history-seed: %s", seed)

    hitl_stats = hitl_sync.repair_hitl_queue()
    if hitl_stats.get("enqueued") or hitl_stats.get("reconciled_plans"):
        log.info("hitl-repair: %s", hitl_stats)

    # Multi-asset update added a boot-time consolidation of duplicate
    # active plans. Idempotent; safe to run before stale-run recovery.
    plan_dedup = db.consolidate_duplicate_active_plans()
    if plan_dedup.get("plans_closed"):
        log.info("active-plan-dedup: %s", plan_dedup)

    stale = db.recover_stale_running_runs(max_age_sec=900)
    if stale.get("closed_stale_running"):
        log.info("stale-runs-closed: %s", stale)

    rag_bootstrap.ensure_ready()

    from .. import llm
    if llm.probe_at_startup():
        log.info("llm-probe: live OpenAI OK")
    elif llm.is_mock():
        log.info("llm-probe: mock mode (no key, USE_MOCK_LLM, or API unavailable)")

    recovered = firm_orchestration.recover_stale_balance_runs()
    if recovered:
        log.info("balance-recovered: %s", recovered)


def _start_background_tasks() -> list[asyncio.Task]:
    """Start scheduler / balance loops; return tasks for later cancel."""
    cfg = get_settings()
    tasks: list[asyncio.Task] = []
    if not cfg.RUN_SCHEDULER_IN_API:
        log.info("scheduler-skipped: RUN_SCHEDULER_IN_API=false (deploy worker container)")
        return tasks

    if cfg.MARKET_CADENCE_ENABLED:
        from ..scheduler import market_cadence_loop
        tasks.append(asyncio.create_task(market_cadence_loop(), name="market-cadence"))
        log.info("scheduler-started: market_cadence_loop")

    if cfg.AUTO_PLAN_SUPERVISION:
        from ..scheduler import plan_supervision_loop
        tasks.append(asyncio.create_task(plan_supervision_loop(), name="plan-supervision"))
        log.info("scheduler-started: plan_supervision_loop")

    if (
        not cfg.MARKET_CADENCE_ENABLED
        and cfg.FIRM_BALANCE_INTERVAL_SEC >= 300
        and cfg.FIRM_MANAGER_AUTO_TRIGGER
    ):
        from ..scheduler import firm_balance_loop
        tasks.append(asyncio.create_task(firm_balance_loop(), name="firm-balance"))
        log.info("scheduler-started: firm_balance_loop")

    return tasks


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    await _bootstrap()
    tasks = _start_background_tasks()
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover
                log.warning("task-shutdown-error name=%s err=%s", t.get_name(), e)
        log.info("shutdown-complete")


async def bootstrap_only() -> None:
    """Entry point for the worker container — boot without HTTP server."""
    setup_logging()
    await _bootstrap()
