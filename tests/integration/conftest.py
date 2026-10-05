"""Integration tests run against a dedicated database so they never touch dev data.

TEST_DATABASE_URL wins; otherwise `<DATABASE_URL db name>_test` is created and migrated on demand.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import text


def _test_url() -> str:
    from prospectpilot.config import Settings

    base = Settings().database_url
    return (
        os.environ.get("TEST_DATABASE_URL")
        or base.rsplit("/", 1)[0] + "/" + base.rsplit("/", 1)[1] + "_test"
    )


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    import psycopg

    from prospectpilot.config import get_settings

    url = _test_url()
    admin = url.replace("postgresql+psycopg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"
    name = url.rsplit("/", 1)[1]
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError:
        pytest.skip("postgres not reachable (run `make up`)")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    from alembic import command
    from alembic.config import Config

    from prospectpilot.config import REPO_ROOT

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    from prospectpilot.memory import db

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
