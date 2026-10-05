from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from prospectpilot.llm.embeddings import HashEmbedder
from prospectpilot.memory import repo
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import FactRow
from prospectpilot.models import CompanyCandidate, ExtractedFact

pytestmark = pytest.mark.integration


async def test_store_facts_dedupes_and_supports_similarity() -> None:
    emb = HashEmbedder(768)
    facts = [
        ExtractedFact(kind="news", text="Acme launched Pipelines 2.0 with column lineage.",
                      source_url="https://acme.io/blog/p2", published_at=date(2026, 8, 12)),
        ExtractedFact(kind="open_role", text="Acme is hiring a Senior Platform Engineer.",
                      source_url="https://acme.io/careers"),
    ]  # fmt: skip
    async with session_scope() as s:
        company = await repo.upsert_company(
            s, CompanyCandidate(name="Acme", domain="acme.io", source="csv", source_url="csv:x")
        )
        rows = await repo.store_facts(s, company.id, facts, emb)
        again = await repo.store_facts(s, company.id, facts, emb)  # idempotent
        assert len(rows) == len(again) == 2
        assert all(r.embedding is not None for r in rows)

        (q,) = await emb.embed(["which roles is Acme hiring for platform engineer"])
        nearest = await s.scalar(
            select(FactRow).order_by(FactRow.embedding.cosine_distance(q)).limit(1)
        )
        assert nearest is not None and nearest.kind == "open_role"

        same = await repo.upsert_company(
            s, CompanyCandidate(name="Acme Inc", domain="www.acme.io", source="hn",
                                source_url="hn", signals=["hiring"])
        )  # fmt: skip
        assert same.id == company.id and same.signals == ["hiring"]
