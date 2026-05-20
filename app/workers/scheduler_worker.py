"""Scheduler worker entry point.

Runs the firm's background loops (plan supervision, firm balance) in a
dedicated container so the API can be horizontally scaled without
duplicating ticks. Configuration via env vars (see app.core.settings).

Run with:
    python -m app.workers.scheduler_worker
"""
from __future__ import annotations

import asyncio
import signal

from ..core.lifecycle import bootstrap_only
from ..core.logging import get_logger
from ..core.settings import get_settings

log = get_logger("horizon.worker.scheduler")


async def _main() -> None:
    await bootstrap_only()
    cfg = get_settings()

    from .metrics_server import start_metrics_server
    start_metrics_server(port=cfg.WORKER_METRICS_PORT)

    tasks: list[asyncio.Task] = []
    if cfg.MARKET_CADENCE_ENABLED:
        from ..scheduler import market_cadence_loop
        tasks.append(asyncio.create_task(market_cadence_loop(), name="market-cadence"))
        log.info("started market_cadence_loop (ET wall-clock jobs)")

    if cfg.AUTO_PLAN_SUPERVISION:
        from ..scheduler import plan_supervision_loop
        tasks.append(asyncio.create_task(plan_supervision_loop(), name="plan-supervision"))
        log.info("started plan_supervision_loop interval=%ss", cfg.PLAN_SUPERVISION_INTERVAL_SEC)

    if (
        not cfg.MARKET_CADENCE_ENABLED
        and cfg.FIRM_BALANCE_INTERVAL_SEC >= 300
        and cfg.FIRM_MANAGER_AUTO_TRIGGER
    ):
        from ..scheduler import firm_balance_loop
        tasks.append(asyncio.create_task(firm_balance_loop(), name="firm-balance"))
        log.info("started firm_balance_loop interval=%ss", cfg.FIRM_BALANCE_INTERVAL_SEC)

    if not tasks:
        log.warning("no scheduler tasks enabled — exiting")
        return

    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("signal received — shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # Windows / non-main thread
            pass

    await stop.wait()
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    log.info("scheduler worker stopped")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
