"""Background scheduler — market-hours cadence + optional interval loops."""
from __future__ import annotations

import asyncio
import time

from . import config, graph
from .core.logging import get_logger
from .daily_plan import load

log = get_logger("horizon.scheduler")


async def market_cadence_loop() -> None:
    """Wall-clock ET jobs (pre-open, open, mid-morning, pre-close, EOD)."""
    from . import daily_cadence
    from .market_calendar import due_jobs, is_trading_day, seconds_until_next_event

    log.info("market-cadence-loop-started")
    while True:
        completed: set[str] | None = None
        try:
            if is_trading_day():
                plan = load()
                completed = set(plan.get("completed_jobs") or [])
                for job_id in due_jobs(completed=completed):
                    await asyncio.to_thread(daily_cadence.run_job, job_id)
                    plan = load()
                    completed = set(plan.get("completed_jobs") or [])
            sleep_sec = min(60.0, seconds_until_next_event(completed=completed))
        except Exception as e:
            log.exception("market-cadence-tick-failed: %s", e)
            sleep_sec = 60.0
        await asyncio.sleep(sleep_sec)


async def firm_balance_loop() -> None:
    if config.MARKET_CADENCE_ENABLED:
        log.info("firm-balance-loop-skipped: MARKET_CADENCE_ENABLED")
        return
    if not config.FIRM_MANAGER_AUTO_TRIGGER:
        return
    interval = int(config.FIRM_BALANCE_INTERVAL_SEC)
    if interval < 300:
        return
    from . import firm_orchestration

    log.info("firm-balance-loop interval=%ss", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(
                firm_orchestration.run_balance_cycle,
                trigger_supervision=False,
            )
        except Exception as e:
            log.exception("firm-balance-cycle-failed: %s", e)


async def plan_supervision_loop() -> None:
    if not config.AUTO_PLAN_SUPERVISION:
        return
    from .market_calendar import is_equity_session_open

    interval = max(60, int(config.PLAN_SUPERVISION_INTERVAL_SEC))
    log.info(
        "plan-supervision-loop interval=%ss auto_execute=%s cadence=%s",
        interval,
        config.AUTO_PLAN_EXECUTE,
        config.MARKET_CADENCE_ENABLED,
    )
    last_tick = 0.0
    while True:
        await asyncio.sleep(30)
        if time.time() - last_tick < interval:
            continue
        if config.MARKET_CADENCE_ENABLED and not is_equity_session_open():
            if not config.SKIP_MARKET_HOURS_CADENCE:
                continue
        last_tick = time.time()
        try:
            await asyncio.to_thread(
                graph.start_plan_supervision,
                spawn_pipeline=config.AUTO_PLAN_SPAWN_PIPELINE,
                auto_execute=config.AUTO_PLAN_EXECUTE,
            )
        except Exception as e:
            log.exception("supervision-cycle-failed: %s", e)
