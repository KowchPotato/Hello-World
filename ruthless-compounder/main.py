#!/usr/bin/env python3
"""
RuthlessCompounder — Main Entry Point

Starts the daily compounding scheduler and the risk sentinel background loop.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import structlog
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

from agent.core_loop import run_daily_compound
from agent.risk_sentinel import RiskSentinel
from agent.tx_budget import TxBudget
from agent.utils import init_db, load_config, is_paused

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(os.getenv("LOG_LEVEL", "INFO"))
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger("ruthless.main")


# ---------------------------------------------------------------------------
# Graceful Shutdown
# ---------------------------------------------------------------------------
shutdown_event = asyncio.Event()


def _handle_signal(sig: signal.Signals) -> None:
    log.warning("shutdown_signal_received", signal=sig.name)
    shutdown_event.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    """Start scheduler with daily compound + risk sentinel loops."""
    config = load_config()
    mode = config.get("mode", "testnet")
    log.info("starting_ruthless_compounder", mode=mode)

    # Initialize SQLite database
    init_db()

    # Transaction budget tracker
    tx_budget = TxBudget(config)
    log.info("tx_budget_status", used=tx_budget.used_this_year(), cap=tx_budget.annual_cap)

    # Risk sentinel
    sentinel = RiskSentinel(config, tx_budget)

    # APScheduler
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Daily compound at configured hour
    compound_hour = config["schedule"]["daily_compound_hour_utc"]
    scheduler.add_job(
        run_daily_compound,
        trigger=CronTrigger(hour=compound_hour, minute=0, timezone="UTC"),
        args=[config, tx_budget],
        id="daily_compound",
        name="Daily AI Compound Cycle",
        max_instances=1,
        misfire_grace_time=3600,  # 1-hour grace if container was down
    )

    # Risk sentinel every N minutes
    sentinel_interval = config["schedule"]["risk_sentinel_interval_min"]
    scheduler.add_job(
        sentinel.check,
        trigger=IntervalTrigger(minutes=sentinel_interval),
        id="risk_sentinel",
        name="Risk Sentinel Poll",
        max_instances=1,
    )

    scheduler.start()
    log.info(
        "scheduler_started",
        daily_compound_utc=f"{compound_hour:02d}:00",
        sentinel_interval_min=sentinel_interval,
    )

    # Register OS signals for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _handle_signal(s))

    # Block until shutdown signal
    await shutdown_event.wait()
    scheduler.shutdown(wait=False)
    log.info("ruthless_compounder_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
