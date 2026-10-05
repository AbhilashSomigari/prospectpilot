"""FastAPI application: campaigns, runs, prospects, improvements, metrics."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from prospectpilot.config import get_settings
from prospectpilot.memory import campaigns as cmp
from prospectpilot.memory import db
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import (
    Campaign,
    Company,
    Draft,
    FactRow,
    ImprovementRound,
    OutboxMessage,
    Person,
    Prospect,
    Reply,
    Run,
)
from prospectpilot.models import ICP
from prospectpilot.obs import metrics as _metrics  # noqa: F401  (registers collectors)
from prospectpilot.obs.tracing import setup_tracing, shutdown_tracing


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_tracing("prospectpilot-api")
    yield
    shutdown_tracing()
    await db.dispose()


app = FastAPI(
    title="ProspectPilot",
    version="0.1.0",
    description="Self-improving SDR agent. All sending goes to the Mailpit sandbox; "
    "replies marked simulated=true are SIMULATED.",
    lifespan=lifespan,
)


async def require_key(authorization: Annotated[str | None, Header()] = None) -> None:
    key = get_settings().api_key
    if key is None:
        return
    if authorization != f"Bearer {key.get_secret_value()}":
        raise HTTPException(status_code=401, detail="invalid or missing API key")


Auth = Depends(require_key)


class RunOptions(BaseModel):
    offline: bool = False
    replay_dirs: list[str] = Field(default_factory=list)
    dns_fixture: str | None = None
    send: bool = True


class Created(BaseModel):
    id: str
    status: str = "created"


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "db": "ok" if await db.ping() else "unreachable"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/campaigns", status_code=201, dependencies=[Auth])
async def create_campaign(icp: ICP) -> Created:
    async with session_scope() as s:
        c = await cmp.create_campaign(s, icp)
        return Created(id=str(c.id))


@app.get("/campaigns/{campaign_id}", dependencies=[Auth])
async def get_campaign(campaign_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        c = await s.get(Campaign, campaign_id)
        if c is None:
            raise HTTPException(404, "campaign not found")
        runs = list(
            await s.scalars(
                select(Run).where(Run.campaign_id == campaign_id).order_by(Run.created_at)
            )
        )
        counts = dict(
            (
                await s.execute(
                    select(Prospect.status, func.count())
                    .where(Prospect.campaign_id == campaign_id)
                    .group_by(Prospect.status)
                )
            ).all()
        )
        return {
            "id": str(c.id),
            "name": c.name,
            "icp": c.icp,
            "created_at": c.created_at,
            "prospects_by_status": counts,
            "runs": [
                {"id": str(r.id), "status": r.status, "created_at": r.created_at} for r in runs
            ],
        }


@app.post("/campaigns/{campaign_id}/run", status_code=202, dependencies=[Auth])
async def start_run(
    campaign_id: uuid.UUID,
    background: BackgroundTasks,
    options: RunOptions | None = None,
    inline: bool = False,
) -> Created:
    """Queue a run for the worker, or (inline=true) execute it in this API process."""
    async with session_scope() as s:
        if await s.get(Campaign, campaign_id) is None:
            raise HTTPException(404, "campaign not found")
        run = await cmp.create_run(s, campaign_id, (options or RunOptions()).model_dump())
        run_id = run.id
    if inline:
        from prospectpilot.graph.runner import execute_run

        background.add_task(execute_run, run_id)
    return Created(id=str(run_id), status="queued")


def _jaeger_url(trace_id: str | None) -> str | None:
    return f"http://localhost:16686/trace/{trace_id}" if trace_id else None


@app.get("/runs/{run_id}", dependencies=[Auth])
async def get_run(run_id: uuid.UUID) -> dict[str, Any]:
    async with session_scope() as s:
        r = await s.get(Run, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return {
            "id": str(r.id),
            "campaign_id": str(r.campaign_id),
            "status": r.status,
            "options": r.options,
            "node_timings_ms": r.node_timings,
            "stats": r.stats,
            "trace_id": r.trace_id,
            "trace_url": _jaeger_url(r.trace_id),
            "error": r.error,
            "created_at": r.created_at,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
        }


@app.get("/prospects/{prospect_id}", dependencies=[Auth])
async def get_prospect(prospect_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        p = await s.get(Prospect, prospect_id)
        if p is None:
            raise HTTPException(404, "prospect not found")
        company = await s.get(Company, p.company_id)
        assert company is not None
        person = await s.get(Person, p.person_id) if p.person_id else None
        facts = list(
            await s.scalars(
                select(FactRow).where(FactRow.company_id == company.id).order_by(FactRow.id)
            )
        )
        drafts = list(
            await s.scalars(select(Draft).where(Draft.prospect_id == p.id).order_by(Draft.id))
        )
        outbox = list(
            await s.scalars(
                select(OutboxMessage)
                .where(OutboxMessage.prospect_id == p.id)
                .order_by(OutboxMessage.step)
            )
        )
        replies = list(await s.scalars(select(Reply).where(Reply.prospect_id == p.id)))
        return {
            "id": p.id,
            "status": p.status,
            "company": {
                "id": company.id,
                "name": company.name,
                "domain": company.domain,
                "source": company.source,
                "source_url": company.source_url,
            },
            "person": {"name": person.full_name, "title": person.title} if person else None,
            "verification": {
                "email": p.email,
                "confidence": p.email_confidence,
                **(p.verification or {}),
            },
            "facts": [
                {
                    "id": f.id,
                    "kind": f.kind,
                    "text": f.text,
                    "source_url": f.source_url,
                    "fetched_at": f.fetched_at,
                    "published_at": f.published_at,
                }
                for f in facts
            ],
            "drafts": [
                {
                    "id": d.id,
                    "round": d.round,
                    "passed": d.passed,
                    "is_final": d.is_final,
                    "judge_score": d.judge_score,
                    "grounded_claims": d.grounded_claims,
                    "total_claims": d.total_claims,
                    "sequence": d.sequence,
                    "critique": d.critique,
                }
                for d in drafts
            ],
            "outbox": [
                {
                    "id": m.id,
                    "step": m.step,
                    "status": m.status,
                    "to": m.to_email,
                    "subject": m.subject,
                    "scheduled_for": m.scheduled_for,
                    "sent_at": m.sent_at,
                    "smtp_message_id": m.smtp_message_id,
                }
                for m in outbox
            ],
            "replies": [
                {
                    "label": "SIMULATED" if r.simulated else "REAL",
                    "simulated": r.simulated,
                    "outcome": r.outcome,
                    "probability": r.probability,
                    "body": r.body,
                }
                for r in replies
            ],
            "cost_usd": p.cost_usd,
            "processing_ms": p.processing_ms,
        }


class ImproveRequest(BaseModel):
    trials: int = 2
    limit: int | None = None


@app.post("/improve", status_code=202, dependencies=[Auth])
async def improve(req: ImproveRequest | None = None) -> Created:
    """Queue one self-improvement round; the worker picks it up."""
    from prospectpilot.evals.improve import queue_round

    req = req or ImproveRequest()
    round_id = await queue_round(trials=req.trials, limit=req.limit, command="POST /improve")
    return Created(id=str(round_id), status="queued")


@app.get("/improvements", dependencies=[Auth])
async def improvements() -> list[dict[str, Any]]:
    async with session_scope() as s:
        rows = list(await s.scalars(select(ImprovementRound).order_by(ImprovementRound.round)))
        return [
            {
                "round": r.round,
                "status": r.status,
                "decision": r.decision,
                "provider": r.provider,
                "models": r.models,
                "active_prompt_id": r.active_prompt_id,
                "winner_prompt_id": r.winner_prompt_id,
                "metrics_before": r.metrics_before,
                "metrics_after": r.metrics_after,
                "gate_results": r.gate_results,
                "failure_clusters": r.failure_clusters,
                "prompt_diff": r.prompt_diff,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in rows
        ]
