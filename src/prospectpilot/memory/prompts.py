"""Prompt registry: versioned prompts with status candidate | active | retired."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from prospectpilot.memory.tables import Prompt
from prospectpilot.prompts import load as load_prompt

WRITER = "writer"
SEED_FILES = {WRITER: "writer_v1"}


async def get_active(session: AsyncSession, name: str = WRITER) -> Prompt:
    """The active version; seeds v1 from the packaged template on first use."""
    row = await session.scalar(select(Prompt).where(Prompt.name == name, Prompt.status == "active"))
    if row is not None:
        return row
    row = Prompt(
        name=name,
        version=1,
        template=load_prompt(SEED_FILES[name]),
        status="active",
        rationale="seed prompt shipped with the package",
    )
    session.add(row)
    await session.flush()
    return row


async def next_version(session: AsyncSession, name: str) -> int:
    current = await session.scalar(select(func.max(Prompt.version)).where(Prompt.name == name))
    return int(current or 0) + 1


async def add_candidate(
    session: AsyncSession, name: str, template: str, parent_id: int | None, rationale: str
) -> Prompt:
    row = Prompt(
        name=name,
        version=await next_version(session, name),
        template=template,
        status="candidate",
        parent_id=parent_id,
        rationale=rationale,
    )
    session.add(row)
    await session.flush()
    return row


async def promote(session: AsyncSession, prompt_id: int) -> None:
    row = await session.get(Prompt, prompt_id)
    if row is None:
        raise LookupError(f"prompt {prompt_id} not found")
    await session.execute(
        update(Prompt)
        .where(Prompt.name == row.name, Prompt.status == "active")
        .values(status="retired")
    )
    row.status = "active"


async def set_scores(session: AsyncSession, prompt_id: int, scores: dict[str, Any]) -> None:
    row = await session.get(Prompt, prompt_id)
    if row is not None:
        row.eval_scores = {**(row.eval_scores or {}), **scores}


async def retire_candidates(session: AsyncSession, name: str) -> None:
    await session.execute(
        update(Prompt)
        .where(Prompt.name == name, Prompt.status == "candidate")
        .values(status="retired")
    )
