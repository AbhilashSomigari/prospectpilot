"""Background worker: queued campaign runs, queued improvement rounds, due follow-ups."""

from __future__ import annotations

import asyncio
import logging
import time

from prospectpilot.config import get_settings
from prospectpilot.obs.metrics import start_metrics_server
from prospectpilot.obs.tracing import setup_logging, setup_tracing

log = logging.getLogger("prospectpilot.worker")

FOLLOWUP_EVERY_S = 30.0
_last_followup = 0.0


async def tick() -> bool:
    """Do one unit of work. Returns True if something was processed."""
    global _last_followup
    from prospectpilot.agents.mailer import Mailer
    from prospectpilot.agents.outbox import flush_due
    from prospectpilot.evals.improve import claim_queued_round
    from prospectpilot.graph.runner import execute_run
    from prospectpilot.memory import campaigns as cmp
    from prospectpilot.memory.db import session_scope

    async with session_scope() as s:
        run = await cmp.claim_queued_run(s)
        run_id = run.id if run else None
    if run_id is not None:
        log.info("executing run %s", run_id)
        try:
            stats = await execute_run(run_id)
            log.info("run %s succeeded: %s", run_id, stats)
        except Exception:
            log.exception("run %s failed", run_id)
        return True

    claimed = await claim_queued_round()
    if claimed is not None:
        from prospectpilot.evals.improve import execute_round

        log.info("executing improvement round id=%s", claimed["id"])
        await execute_round(claimed["id"])
        return True

    if time.monotonic() - _last_followup > FOLLOWUP_EVERY_S:
        _last_followup = time.monotonic()
        sent = await flush_due(Mailer(get_settings()), get_settings())
        if sent:
            log.info("sent %d due follow-ups", sent)
            return True
    return False


async def run_worker(poll_interval_s: float = 2.0) -> None:
    setup_logging()
    settings = get_settings()
    setup_tracing("prospectpilot-worker")
    start_metrics_server(settings.worker_metrics_port)
    from prospectpilot.llm.client import get_llm
    from prospectpilot.obs.metrics import init_label_sets

    init_label_sets(settings.llm_provider, get_llm().models())
    from prospectpilot.graph.runner import setup_checkpointer

    await setup_checkpointer()
    log.info(
        "worker started; provider=%s metrics on :%s",
        settings.llm_provider,
        settings.worker_metrics_port,
    )
    while True:
        try:
            did_work = await tick()
        except Exception:
            log.exception("worker tick failed")
            did_work = False
        if not did_work:
            await asyncio.sleep(poll_interval_s)
