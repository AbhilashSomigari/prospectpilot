from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from prospectpilot.memory import db


@pytest.fixture(autouse=True)
async def _db() -> AsyncIterator[None]:
    """Integration tests need the compose Postgres (migrated). Each test starts clean."""
    if not await db.ping():
        pytest.skip("postgres not reachable (run `make up`)")
    async with db.get_engine().begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE campaigns, runs, companies, people, prospects, facts, drafts, outbox, "
                "replies, learnings, prompts, improvement_rounds, llm_calls RESTART IDENTITY "
                "CASCADE"
            )
        )
    yield
    await db.dispose()
