"""Repository functions over the ORM: prospect memory (companies, people, facts, drafts, ...)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from prospectpilot.llm.client import UsageTracker
from prospectpilot.llm.embeddings import Embedder
from prospectpilot.memory.tables import (
    Company,
    FactRow,
    LLMCall,
    Person,
    Prospect,
)
from prospectpilot.models import CompanyCandidate, ExtractedFact, Fact


async def upsert_company(session: AsyncSession, cand: CompanyCandidate) -> Company:
    existing = await session.scalar(select(Company).where(Company.domain == cand.domain))
    if existing is not None:
        existing.signals = list(dict.fromkeys([*existing.signals, *cand.signals]))
        if len(cand.description) > len(existing.description):
            existing.description = cand.description
        return existing
    company = Company(
        domain=cand.domain,
        name=cand.name,
        description=cand.description,
        source=cand.source,
        source_url=cand.source_url,
        signals=cand.signals,
        extra={"locations": cand.locations, "team_size": cand.team_size},
    )
    session.add(company)
    await session.flush()
    return company


async def upsert_person(
    session: AsyncSession,
    company_id: int,
    full_name: str,
    title: str | None,
    email: str | None,
    source: str,
    source_url: str | None,
) -> Person:
    existing = await session.scalar(
        select(Person).where(Person.company_id == company_id, Person.full_name == full_name)
    )
    if existing is not None:
        existing.title = existing.title or title
        existing.published_email = existing.published_email or email
        return existing
    person = Person(
        company_id=company_id,
        full_name=full_name,
        title=title,
        published_email=email,
        source=source,
        source_url=source_url,
    )
    session.add(person)
    await session.flush()
    return person


async def add_prospect(
    session: AsyncSession, campaign_id: uuid.UUID, company_id: int, person_id: int | None
) -> Prospect:
    stmt = select(Prospect).where(
        Prospect.campaign_id == campaign_id,
        Prospect.company_id == company_id,
        Prospect.person_id.is_(None) if person_id is None else Prospect.person_id == person_id,
    )
    existing = await session.scalar(stmt)
    if existing is not None:
        return existing
    p = Prospect(campaign_id=campaign_id, company_id=company_id, person_id=person_id)
    session.add(p)
    await session.flush()
    return p


async def store_facts(
    session: AsyncSession,
    company_id: int,
    facts: list[ExtractedFact],
    embedder: Embedder,
    fetched_at: datetime | None = None,
) -> list[FactRow]:
    if not facts:
        return []
    vectors = await embedder.embed([f.text for f in facts])
    now = fetched_at or datetime.now(UTC)
    rows: list[dict[str, Any]] = [
        {
            "company_id": company_id,
            "kind": f.kind,
            "text": f.text,
            "source_url": f.source_url,
            "fetched_at": now,
            "published_at": f.published_at,
            "embedding": vec,
        }
        for f, vec in zip(facts, vectors, strict=True)
    ]
    stmt = insert(FactRow).values(rows).on_conflict_do_nothing(constraint="uq_fact_company_text")
    await session.execute(stmt)
    return await company_facts_rows(session, company_id)


async def company_facts_rows(session: AsyncSession, company_id: int) -> list[FactRow]:
    res = await session.scalars(
        select(FactRow).where(FactRow.company_id == company_id).order_by(FactRow.id)
    )
    return list(res)


async def company_facts(session: AsyncSession, company_id: int) -> list[Fact]:
    return [Fact.model_validate(r) for r in await company_facts_rows(session, company_id)]


async def record_llm_calls(
    session: AsyncSession,
    tracker: UsageTracker,
    *,
    run_id: uuid.UUID | None = None,
    prospect_id: int | None = None,
    scope: str = "campaign",
    trace_id: str | None = None,
) -> None:
    for resp, purpose in zip(tracker.calls, tracker.purposes, strict=True):
        session.add(
            LLMCall(
                run_id=run_id,
                prospect_id=prospect_id,
                scope=scope,
                purpose=purpose,
                provider=resp.provider,
                model=resp.model,
                input_tokens=resp.input_tokens,
                output_tokens=resp.output_tokens,
                cost_usd=resp.cost_usd,
                latency_ms=resp.latency_ms,
                trace_id=trace_id,
            )
        )


async def delete_facts(session: AsyncSession, company_id: int) -> None:
    await session.execute(delete(FactRow).where(FactRow.company_id == company_id))
