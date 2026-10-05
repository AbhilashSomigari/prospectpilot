"""Background worker: picks up queued campaign runs and sends due follow-ups."""

from __future__ import annotations

import asyncio
import logging

from prospectpilot.config import get_settings
from prospectpilot.obs.metrics import start_metrics_server
from prospectpilot.obs.tracing import setup_tracing

log = logging.getLogger("prospectpilot.worker")


async def tick() -> bool:
    """Do one unit of work. Returns True if something was processed."""
    return False


async def run_worker(poll_interval_s: float = 2.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    setup_tracing("prospectpilot-worker")
    start_metrics_server(settings.worker_metrics_port)
    log.info("worker started; metrics on :%s", settings.worker_metrics_port)
    while True:
        try:
            did_work = await tick()
        except Exception:
            log.exception("worker tick failed")
            did_work = False
        if not did_work:
            await asyncio.sleep(poll_interval_s)
