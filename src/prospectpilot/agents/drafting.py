"""Writer ⇄ Critic loop for a single prospect (max N rewrites). Shared by the eval adapter.

The LangGraph pipeline runs the same two functions as separate nodes with a conditional edge;
this helper exists so evals exercise exactly the production writer/critic code.
"""

from __future__ import annotations

from prospectpilot.agents.critic import critique_sequence
from prospectpilot.agents.writer import write_sequence
from prospectpilot.config import Settings
from prospectpilot.llm.base import LLMError
from prospectpilot.llm.client import LLM
from prospectpilot.models import (
    CheckResult,
    Critique,
    DraftAttempt,
    DraftOutcome,
    EmailSequence,
    Offer,
    ProspectContext,
    Sender,
)


def invalid_output_critique(error: str) -> Critique:
    return Critique(
        passed=False,
        deterministic_passed=False,
        checks=[CheckResult(code="invalid_output", passed=False, detail=error[:400])],
        feedback=f"- [invalid_output] {error[:400]}. Reply with valid JSON in the exact schema.",
    )


async def write_round(
    llm: LLM,
    ctx: ProspectContext,
    offer: Offer,
    sender: Sender,
    system_prompt: str,
    settings: Settings,
    round_: int,
    previous: EmailSequence | None,
    critique: Critique | None,
    seed: int | None = None,
) -> DraftAttempt:
    try:
        seq = await write_sequence(
            llm,
            ctx,
            offer,
            sender,
            system_prompt,
            settings,
            round_=round_,
            previous=previous,
            critique=critique,
            seed=None if seed is None else seed + round_,
        )
    except LLMError as exc:
        return DraftAttempt(
            round=round_,
            sequence=None,
            critique=invalid_output_critique(str(exc)),
            raw_error=str(exc),
        )
    crit = await critique_sequence(llm, seq, ctx, offer, settings, round_=round_)
    return DraftAttempt(round=round_, sequence=seq, critique=crit)


async def draft_with_critique(
    llm: LLM,
    ctx: ProspectContext,
    offer: Offer,
    sender: Sender,
    system_prompt: str,
    settings: Settings,
    seed: int | None = None,
) -> DraftOutcome:
    attempts: list[DraftAttempt] = []
    previous: EmailSequence | None = None
    critique: Critique | None = None
    for round_ in range(settings.critic_max_rewrites + 1):
        attempt = await write_round(
            llm, ctx, offer, sender, system_prompt, settings, round_, previous, critique, seed
        )
        attempts.append(attempt)
        if attempt.critique.passed:
            break
        previous = attempt.sequence or previous
        critique = attempt.critique
    return DraftOutcome(attempts=attempts)
