"""Self-improvement rounds (queue + execution). Execution is implemented in M5."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from prospectpilot.config import get_settings
from prospectpilot.llm.client import get_llm
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import ImprovementRound


async def next_round_number() -> int:
    async with session_scope() as s:
        current = await s.scalar(select(func.max(ImprovementRound.round)))
        return int(current if current is not None else -1) + 1


async def queue_round(trials: int = 2, limit: int | None = None, command: str = "") -> int:
    llm = get_llm()
    async with session_scope() as s:
        current = await s.scalar(select(func.max(ImprovementRound.round)))
        row = ImprovementRound(
            round=int(current if current is not None else -1) + 1,
            status="queued",
            provider=get_settings().llm_provider,
            models=llm.models(),
            command=command,
            artifacts={"trials": trials, "limit": limit},
        )
        s.add(row)
        await s.flush()
        return row.id


async def claim_queued_round() -> dict[str, Any] | None:
    async with session_scope() as s:
        row = await s.scalar(
            select(ImprovementRound)
            .where(ImprovementRound.status == "queued")
            .order_by(ImprovementRound.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.status = "running"
        return {"id": row.id, **(row.artifacts or {})}


async def execute_round(round_id: int) -> None:
    """Placeholder until M5 wires the EvalForge loop."""
    async with session_scope() as s:
        row = await s.get(ImprovementRound, round_id)
        if row is not None:
            row.status = "failed"
            row.decision = "not_implemented"
