"""Campaigns, runs, drafts and outbox persistence helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from prospectpilot.memory.tables import (
    Campaign,
    Company,
    Draft,
    OutboxMessage,
    Person,
    Prospect,
    Run,
)
from prospectpilot.models import ICP, Critique, EmailSequence, Fact, ProspectContext


async def create_campaign(session: AsyncSession, icp: ICP) -> Campaign:
    c = Campaign(name=icp.name, icp=icp.model_dump(mode="json"))
    session.add(c)
    await session.flush()
    return c


async def create_run(
    session: AsyncSession, campaign_id: uuid.UUID, options: dict[str, Any] | None = None
) -> Run:
    r = Run(campaign_id=campaign_id, status="queued", options=options or {})
    session.add(r)
    await session.flush()
    return r


async def claim_queued_run(session: AsyncSession) -> Run | None:
    """Atomically claim the oldest queued run (safe with multiple workers)."""
    run = await session.scalar(
        select(Run)
        .where(Run.status == "queued")
        .order_by(Run.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if run is not None:
        run.status = "running"
        run.started_at = datetime.now(UTC)
    return run


async def add_timing(session: AsyncSession, run_id: uuid.UUID, key: str, ms: float) -> None:
    run = await session.get(Run, run_id, with_for_update=True)
    if run is not None:
        run.node_timings = {**(run.node_timings or {}), key: round(ms, 1)}


async def prospect_context(
    session: AsyncSession, prospect: Prospect, learnings: list[str] | None = None
) -> ProspectContext:
    from prospectpilot.memory.repo import company_facts

    company = await session.get(Company, prospect.company_id)
    assert company is not None
    person = await session.get(Person, prospect.person_id) if prospect.person_id else None
    facts: list[Fact] = await company_facts(session, company.id)
    return ProspectContext(
        prospect_id=prospect.id,
        company_name=company.name,
        domain=company.domain,
        company_description=company.description[:600],
        person_name=person.full_name if person else None,
        person_title=person.title if person else None,
        facts=facts,
        learnings=learnings or [],
    )


async def latest_draft(
    session: AsyncSession, prospect_id: int, run_id: uuid.UUID | None = None
) -> Draft | None:
    stmt = select(Draft).where(Draft.prospect_id == prospect_id)
    if run_id is not None:
        stmt = stmt.where(Draft.run_id == run_id)
    return await session.scalar(stmt.order_by(Draft.id.desc()).limit(1))


def draft_sequence(d: Draft) -> EmailSequence | None:
    return EmailSequence.model_validate(d.sequence) if d.sequence else None


def draft_critique(d: Draft) -> Critique | None:
    return Critique.model_validate(d.critique) if d.critique else None


async def schedule_sequence(
    session: AsyncSession,
    prospect: Prospect,
    draft: Draft,
    seq: EmailSequence,
    followup_days: list[int],
    now: datetime | None = None,
) -> list[OutboxMessage]:
    from prospectpilot.models import strip_markers

    existing = await session.scalar(
        select(func.count()).select_from(OutboxMessage).where(OutboxMessage.draft_id == draft.id)
    )
    if existing:
        return []
    assert prospect.email is not None
    now = now or datetime.now(UTC)
    offsets = [0, *followup_days]
    rows = []
    for email, days in zip(seq.emails, offsets, strict=False):
        row = OutboxMessage(
            prospect_id=prospect.id,
            draft_id=draft.id,
            step=email.step,
            to_email=prospect.email,
            subject=strip_markers(email.subject),
            body=strip_markers(email.body),
            status="scheduled",
            scheduled_for=now + timedelta(days=days),
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows
