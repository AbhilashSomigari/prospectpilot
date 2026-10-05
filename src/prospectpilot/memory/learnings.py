"""Company-wide learnings memory, retrieved by the writer via pgvector similarity search."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from prospectpilot.llm.embeddings import Embedder
from prospectpilot.memory.tables import Learning
from prospectpilot.obs.tracing import set_attrs, span


async def add_learning(
    session: AsyncSession,
    embedder: Embedder,
    text: str,
    category: str,
    *,
    source: str = "failure_analysis",
    round_: int | None = None,
    evidence: dict[str, Any] | None = None,
) -> Learning:
    existing = await session.scalar(
        select(Learning).where(Learning.text == text, Learning.active.is_(True))
    )
    if existing is not None:
        return existing
    (vec,) = await embedder.embed([text])
    row = Learning(
        text=text,
        category=category,
        source=source,
        round=round_,
        evidence=evidence or {},
        embedding=vec,
    )
    session.add(row)
    await session.flush()
    return row


async def search(
    session: AsyncSession, embedder: Embedder, query: str, k: int = 3, max_distance: float = 0.95
) -> list[str]:
    with span("memory.learnings.search", **{"pp.k": k}) as s:
        (vec,) = await embedder.embed([query])
        dist = Learning.embedding.cosine_distance(vec)
        rows = (
            await session.execute(
                select(Learning.text, dist.label("d"))
                .where(Learning.active.is_(True), Learning.embedding.is_not(None))
                .order_by(dist)
                .limit(k)
            )
        ).all()
        hits = [str(text) for text, d in rows if d is not None and float(str(d)) <= max_distance]
        set_attrs(s, **{"pp.hits": len(hits)})
        return hits


async def all_active(session: AsyncSession) -> list[Learning]:
    return list(
        await session.scalars(
            select(Learning).where(Learning.active.is_(True)).order_by(Learning.id)
        )
    )
