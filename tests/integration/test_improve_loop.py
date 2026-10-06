"""A full self-improvement round in mock mode against the isolated test database."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from prospectpilot.evals.fixtures import load_all
from prospectpilot.evals.improve import run_round
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import ImprovementRound, Learning, Prompt

pytestmark = pytest.mark.integration


async def test_improve_round_mock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if len(load_all()) < 4:
        pytest.skip("eval fixtures not built")
    import prospectpilot.evals.improve as improve_mod
    import prospectpilot.llm.client as client

    monkeypatch.setattr(client, "_default", client.LLM(provider="mock"))
    # keep test artifacts out of the repo's evals/runs
    monkeypatch.setattr(improve_mod, "_artifact_dir", lambda n: tmp_path / f"round-{n:02d}")
    (tmp_path / "round-01").mkdir()
    summary = await run_round(trials=1, limit=4)

    assert summary["round"] == 1
    assert len(summary["candidates"]) == 3
    # the mock writer ignores the prompt, so candidates tie with ACTIVE → strict gate keeps ACTIVE
    assert summary["decision"] == "kept_active"
    async with session_scope() as s:
        row = await s.scalar(select(ImprovementRound))
        assert row is not None and row.status == "completed"
        assert row.metrics_before["trials"] == 4 and row.gate_results
        assert row.prompt_diff.startswith("--- writer-v1")
        prompts = list(await s.scalars(select(Prompt).order_by(Prompt.version)))
        assert [p.status for p in prompts] == ["active", "retired", "retired", "retired"]
        assert "round_1" in prompts[0].eval_scores
        assert (tmp_path / "round-01" / "summary.json").exists()
        assert (tmp_path / "round-01" / "active-v1.json").exists()
        learnings = list(await s.scalars(select(Learning)))
        assert all(lr.round == 1 for lr in learnings)
