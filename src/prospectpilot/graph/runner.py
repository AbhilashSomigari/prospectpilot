"""Execute a queued run through the graph with Postgres checkpointing (resumable by run id)."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select

from prospectpilot.config import get_settings
from prospectpilot.graph.build import build_graph
from prospectpilot.graph.deps import PipelineDeps, build_deps
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import Draft, OutboxMessage, Prospect, Reply, Run
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import current_trace_id, set_attrs, setup_tracing, span

log = logging.getLogger(__name__)


async def setup_checkpointer() -> None:
    async with AsyncPostgresSaver.from_conn_string(get_settings().libpq_database_url) as saver:
        await saver.setup()


async def run_stats(run_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        run = await s.get(Run, run_id)
        assert run is not None
        prospects = list(
            await s.scalars(select(Prospect).where(Prospect.campaign_id == run.campaign_id))
        )
        drafts = list(await s.scalars(select(Draft).where(Draft.run_id == run_id)))
        sent = await s.scalar(
            select(func.count())
            .select_from(OutboxMessage)
            .join(Draft, Draft.id == OutboxMessage.draft_id)
            .where(Draft.run_id == run_id, OutboxMessage.status == "sent")
        )
        sim = await s.scalar(
            select(func.count())
            .select_from(Reply)
            .where(
                Reply.prospect_id.in_([p.id for p in prospects] or [-1]),
                Reply.simulated.is_(True),
                Reply.outcome != "no_reply",
            )
        )
    first = [d for d in drafts if d.round == 0]
    final = [d for d in drafts if d.is_final]
    by_status: dict[str, int] = {}
    for p in prospects:
        by_status[p.status] = by_status.get(p.status, 0) + 1
    return {
        "prospects": len(prospects),
        "by_status": by_status,
        "verified_ge_0_7": sum(1 for p in prospects if (p.email_confidence or 0) >= 0.7),
        "drafted": len(final),
        "first_draft_pass": sum(1 for d in first if d.passed),
        "final_pass": sum(1 for d in final if d.passed),
        "claims_grounded": sum(d.grounded_claims for d in final),
        "claims_total": sum(d.total_claims for d in final),
        "emails_sent": int(sent or 0),
        "simulated_replies": int(sim or 0),
        "cost_usd": round(sum(p.cost_usd or 0.0 for p in prospects), 6),
    }


async def execute_run(run_id: uuid.UUID, deps: PipelineDeps | None = None) -> dict[str, Any]:
    setup_tracing()  # idempotent; guarantees a recording span (and trace id) per run
    async with session_scope() as s:
        run = await s.get(Run, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        campaign_id, options = run.campaign_id, dict(run.options or {})
        run.status = "running"
        run.started_at = run.started_at or datetime.now(UTC)
        run.error = None
    own_deps = deps is None
    deps = deps or build_deps(options)
    config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 50}
    try:
        with span(
            "campaign.run",
            **{
                "pp.run_id": str(run_id),
                "pp.campaign_id": str(campaign_id),
                "pp.provider": deps.llm.provider,
            },
        ) as root:
            trace_id = current_trace_id()
            async with session_scope() as s:
                run = await s.get(Run, run_id)
                assert run is not None
                run.trace_id = trace_id
            async with AsyncPostgresSaver.from_conn_string(
                deps.settings.libpq_database_url
            ) as saver:
                graph = build_graph(deps, checkpointer=saver)
                existing = await saver.aget_tuple(config)  # type: ignore[arg-type]
                resume = existing is not None and bool(existing.checkpoint.get("channel_values"))
                state_in = (
                    None if resume else {"run_id": str(run_id), "campaign_id": str(campaign_id)}
                )
                set_attrs(root, **{"pp.resumed": resume})
                await graph.ainvoke(state_in, config=config)
        stats = await run_stats(run_id)
        async with session_scope() as s:
            run = await s.get(Run, run_id)
            assert run is not None
            run.status = "succeeded"
            run.finished_at = datetime.now(UTC)
            run.stats = stats
        metrics.RUNS.labels("succeeded").inc()
        return stats
    except Exception as exc:
        log.exception("run %s failed", run_id)
        async with session_scope() as s:
            run = await s.get(Run, run_id)
            if run is not None:
                run.status = "failed"
                run.finished_at = datetime.now(UTC)
                run.error = f"{type(exc).__name__}: {exc}"[:2000]
        metrics.RUNS.labels("failed").inc()
        raise
    finally:
        if own_deps:
            await deps.aclose()
