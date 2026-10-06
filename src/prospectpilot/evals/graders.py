"""ProspectPilot graders, registered into EvalForge's grader registry.

EvalForge has no grader plugin mechanism (candidate upstream PR: entry-point based grader
plugins); `run_grader` looks types up in `evalforge.graders.deterministic.REGISTRY` at call time,
so adding classes there makes them usable from suite YAML in-process.

Graders re-derive verdicts from the agent's *output* and the frozen fixture — they don't trust the
agent's self-reported critique — except the judge score, which is read from metadata to avoid
paying for a second judge call (same model + rubric as the critic).
"""

from __future__ import annotations

from typing import Any

from evalforge.graders.base import Grader
from evalforge.graders.deterministic import REGISTRY
from evalforge.models import AgentOutput, GradeResult, GraderSpec, TaskSpec
from pydantic import ValidationError

from prospectpilot.agents.critic import deterministic_checks
from prospectpilot.config import get_settings
from prospectpilot.evals.fixtures import load
from prospectpilot.models import EmailSequence


def _result(spec: GraderSpec, score: float, reason: str, **meta: Any) -> GradeResult:
    score = max(0.0, min(1.0, float(score)))
    return GradeResult(
        grader=spec.name or spec.type, score=score, passed=score >= spec.pass_threshold,
        reason=reason, metadata=meta,
    )  # fmt: skip


def _parse(output: AgentOutput) -> EmailSequence | None:
    try:
        return EmailSequence.model_validate_json(output.output)
    except (ValidationError, ValueError):
        return None


class SequenceChecksGrader(Grader):
    """All deterministic critic checks pass on the final output (independently recomputed)."""

    async def grade(self, task: TaskSpec, output: AgentOutput, spec: GraderSpec) -> GradeResult:
        seq = _parse(output)
        if seq is None:
            return _result(
                spec, 0, "output is not a valid 3-email sequence", failures=["invalid_output"]
            )
        fx = load(str(task.metadata.get("fixture", task.id)))
        checks, _ = deterministic_checks(seq, fx.context(), fx.offer, get_settings())
        failed = sorted({c.code for c in checks if not c.passed})
        return _result(spec, 0 if failed else 1, f"failed: {failed}" if failed else "all checks pass",
                       failures=failed)  # fmt: skip


class ClaimGroundingGrader(Grader):
    """Fraction of personalized claims grounded in this prospect's facts (1.0 = no hallucination).

    Named `hallucination_*` in the suite so EvalForge's hallucination_rate = 1 - mean(score).
    """

    async def grade(self, task: TaskSpec, output: AgentOutput, spec: GraderSpec) -> GradeResult:
        seq = _parse(output)
        if seq is None:
            return _result(spec, 0, "no sequence", grounded=0, total=0)
        fx = load(str(task.metadata.get("fixture", task.id)))
        _, claims = deterministic_checks(seq, fx.context(), fx.offer, get_settings())
        grounded = sum(1 for c in claims if c.grounded)
        total = len(claims)
        score = grounded / total if total else 0.0  # no personalized claims = not personalized
        bad = [c.reason for c in claims if not c.grounded][:3]
        return _result(spec, score, f"{grounded}/{total} claims grounded; {bad}",
                       grounded=grounded, total=total)  # fmt: skip


class JudgeScoreGrader(Grader):
    """LLM-judge rubric mean (1-5) from the critic, normalized to 0-1."""

    async def grade(self, task: TaskSpec, output: AgentOutput, spec: GraderSpec) -> GradeResult:
        mean = output.metadata.get("judge_mean")
        if mean is None:
            return _result(spec, 0, "no judge score (writer output invalid or judge failed)")
        return _result(spec, (float(mean) - 1) / 4, f"judge mean {float(mean):.2f}/5",
                       judge_mean=float(mean))  # fmt: skip


CUSTOM_GRADERS: dict[str, type[Grader]] = {
    "pp_sequence_checks": SequenceChecksGrader,
    "pp_claim_grounding": ClaimGroundingGrader,
    "pp_judge_score": JudgeScoreGrader,
}


def register() -> None:
    REGISTRY.update(CUSTOM_GRADERS)
