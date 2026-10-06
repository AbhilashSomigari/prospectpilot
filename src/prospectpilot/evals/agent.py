"""EvalForge adapter for ProspectPilot.

Two entry points onto the SAME code path (Writer ⇄ Critic loop, `agents/drafting.py`):

* `ProspectPilotAgent` — an in-process `evalforge.adapters.base.AgentAdapter` used by
  `prospectpilot eval` / `improve` (fast: no process spawn per trial, shared LLM client).
* `python -m prospectpilot.evals.agent [--prompt-file F]` — EvalForge's command protocol:
  one TaskSpec JSON on stdin → one AgentOutput JSON on stdout, for plain `evalforge run --agent`.

The agent output is the final sequence JSON (with [fact:id] markers); metadata carries the
critic's verdicts so graders can score without paying for a second judge call.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

from evalforge.adapters.base import AgentAdapter
from evalforge.models import AgentOutput, TaskSpec, ToolCall, TraceEvent, Usage

from prospectpilot.agents.drafting import draft_with_critique
from prospectpilot.config import Settings, get_settings
from prospectpilot.evals.fixtures import load
from prospectpilot.llm.client import LLM, track_usage
from prospectpilot.llm.embeddings import Embedder, get_embedder


async def top_learnings(
    embedder: Embedder, learnings: list[str], query: str, k: int = 3
) -> list[str]:
    """In-memory equivalent of the production pgvector search over a frozen learnings snapshot."""
    if not learnings:
        return []
    vecs = await embedder.embed([query, *learnings])
    q, rest = vecs[0], vecs[1:]
    scored = sorted(
        zip(learnings, rest, strict=True),
        key=lambda t: -sum(a * b for a, b in zip(q, t[1], strict=True)),
    )
    return [text for text, _ in scored[:k]]


class ProspectPilotAgent(AgentAdapter):
    def __init__(
        self,
        prompt: str,
        *,
        name: str = "prospectpilot-writer",
        learnings: list[str] | None = None,
        llm: LLM | None = None,
        settings: Settings | None = None,
        base_seed: int | None = None,
    ) -> None:
        self.prompt = prompt
        self.name = name
        self.learnings = learnings or []
        self.settings = settings or get_settings()
        self.llm = llm or LLM(self.settings)
        self.embedder = get_embedder(self.settings)
        self.base_seed = self.settings.llm_seed if base_seed is None else base_seed
        self._calls: dict[str, int] = defaultdict(int)

    def _trial_seed(self, task_id: str) -> int:
        # EvalForge doesn't pass the trial index; the n-th call for a task is trial n. Every
        # variant therefore sees the same set of seeds per task ("same seeds" across variants).
        n = self._calls[task_id]
        self._calls[task_id] += 1
        return self.base_seed + 1000 * n

    async def run(self, task: TaskSpec) -> AgentOutput:
        fx = load(str(task.metadata.get("fixture", task.id)))
        base = fx.context()
        query = f"{base.person_title or ''} {base.company_description}"
        ctx = base.model_copy(
            update={"learnings": await top_learnings(self.embedder, self.learnings, query)}
        )
        seed = self._trial_seed(task.id)
        started = time.perf_counter()
        with track_usage() as usage:
            outcome = await draft_with_critique(
                self.llm, ctx, fx.offer, fx.sender, self.prompt, self.settings, seed=seed
            )
        latency_ms = (time.perf_counter() - started) * 1000
        final = outcome.final
        trajectory: list[TraceEvent] = []
        tool_calls: list[ToolCall] = []
        for a in outcome.attempts:
            tool_calls.append(ToolCall(name="writer", arguments={"round": a.round}))
            tool_calls.append(
                ToolCall(
                    name="critic",
                    arguments={"round": a.round},
                    result={"passed": a.critique.passed, "failures": a.critique.failure_codes},
                )
            )
            trajectory.append(
                TraceEvent(
                    type="state",
                    content=json.dumps(
                        {"round": a.round, "passed": a.critique.passed,
                         "failures": a.critique.failure_codes,
                         "judge_mean": a.critique.judge.mean if a.critique.judge else None}
                    ),
                )
            )  # fmt: skip
        judge = final.critique.judge
        output = final.sequence.model_dump_json() if final.sequence else ""
        return AgentOutput(
            output=output,
            trajectory=trajectory,
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
            ),
            model=self.llm.model_for("writer"),
            metadata={
                "fixture": fx.id,
                "seed": seed,
                "attempts": len(outcome.attempts),
                "first_passed": outcome.first_passed,
                "passed": outcome.passed,
                "first_failures": outcome.attempts[0].critique.failure_codes,
                "final_failures": final.critique.failure_codes,
                "judge": judge.model_dump() if judge else None,
                "judge_mean": judge.mean if judge else None,
                "grounded_claims": final.critique.grounded_claims,
                "total_claims": len(final.critique.claims),
                "feedback": final.critique.feedback,
                "latency_ms": latency_ms,
                "llm_calls": len(usage.calls),
            },
        )


def _nan_safe(x: float | None) -> float | None:
    return None if x is None or math.isnan(x) else x


async def _main_async(prompt_file: str | None) -> None:
    task = TaskSpec.model_validate_json(sys.stdin.read())
    if prompt_file:
        prompt = await asyncio.to_thread(Path(prompt_file).read_text, encoding="utf-8")
    else:
        from prospectpilot.memory import prompts as registry
        from prospectpilot.memory.db import dispose, session_scope

        async with session_scope() as s:
            prompt = (await registry.get_active(s)).template
        await dispose()
    out = await ProspectPilotAgent(prompt).run(task)
    sys.stdout.write(out.model_dump_json())


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="EvalForge command adapter for ProspectPilot")
    ap.add_argument("--prompt-file", default=None, help="writer prompt; default = ACTIVE in DB")
    args = ap.parse_args()
    asyncio.run(_main_async(args.prompt_file))


if __name__ == "__main__":
    main()
