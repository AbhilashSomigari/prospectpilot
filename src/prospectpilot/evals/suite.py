"""Build the EvalForge suite over the frozen fixtures, and run it in-process."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

import yaml
from evalforge.models import EvalRun, GateSpec, GraderSpec, SuiteSpec, TaskSpec
from evalforge.runner import EvalRunner
from evalforge.storage import save_run

from prospectpilot.evals import graders
from prospectpilot.evals.agent import ProspectPilotAgent
from prospectpilot.evals.fixtures import load_all
from prospectpilot.llm.client import LLM

SUITE_NAME = "prospectpilot-writer"
JUDGE_PASS = 0.625  # (3.5 - 1) / 4 — same bar as the critic (CRITIC_MIN_JUDGE=3.5)

GRADERS = [
    GraderSpec(type="json_contract", name="json_contract", config={"required_keys": ["emails"]}),
    GraderSpec(type="citation_groundedness", name="cites_known_fact"),
    GraderSpec(type="pp_sequence_checks", name="sequence_checks"),
    GraderSpec(type="pp_claim_grounding", name="hallucination_grounding"),
    GraderSpec(type="pp_judge_score", name="judge_rubric", pass_threshold=JUDGE_PASS),
]
# Absolute floor for `make eval` / CI. Promotion gates in the improve loop are relative
# to the ACTIVE prompt (see evals/improve.py).
GATES = [
    GateSpec(metric="task_success", op=">=", value=0.5),
    GateSpec(metric="hallucination_rate", op="<=", value=0.25),
]


def build_suite(trials: int = 2, limit: int | None = None, concurrency: int = 4) -> SuiteSpec:
    fixtures = list(load_all().values())[:limit] if limit else list(load_all().values())
    tasks = [
        TaskSpec(
            id=fx.id,
            input=(
                f"Write a 3-step outbound sequence to {fx.person['name'] if fx.person else 'the team'}"
                f" ({fx.person['title'] if fx.person else 'unknown title'}) at {fx.company['name']}"
                f" selling {fx.offer.product}."
            ),
            metadata={"fixture": fx.id, "source_ids": [f"[fact:{f.id}]" for f in fx.facts]},
        )
        for fx in fixtures
    ]
    return SuiteSpec(
        name=SUITE_NAME,
        description="Grounded-and-passing rate of the writer⇄critic loop over frozen fixtures.",
        trials_per_task=trials,
        concurrency=concurrency,
        graders=GRADERS,
        gates=GATES,
        tasks=tasks,
    )


def write_suite_yaml(path: Path, suite: SuiteSpec, builtin_only: bool = False) -> None:
    data = suite.model_dump(mode="json", exclude_defaults=False)
    if builtin_only:
        data["graders"] = [g for g in data["graders"] if not str(g["type"]).startswith("pp_")]
        data["name"] = SUITE_NAME + "-builtin"
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=100))


async def run_suite(
    prompt: str,
    *,
    trials: int = 2,
    limit: int | None = None,
    learnings: list[str] | None = None,
    agent_name: str = "writer",
    out: Path | None = None,
    llm: LLM | None = None,
    concurrency: int = 4,
) -> EvalRun:
    graders.register()
    suite = build_suite(trials=trials, limit=limit, concurrency=concurrency)
    agent = ProspectPilotAgent(prompt, name=agent_name, learnings=learnings, llm=llm)
    run = await EvalRunner(suite, agent).run()
    run.metadata.update({"provider": agent.llm.provider, "models": agent.llm.models(),
                         "learnings": learnings or [], "metrics": summarize(run)})  # fmt: skip
    if out is not None:
        save_run(run, out)
    return run


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(q * (len(values) - 1))))
    return values[k]


def summarize(run: EvalRun) -> dict[str, Any]:
    """ProspectPilot metrics on top of EvalForge's RunSummary (all from the run artifact)."""
    rs = run.results
    meta = [r.output.metadata for r in rs]
    judge = [float(m["judge_mean"]) for m in meta if m.get("judge_mean") is not None]
    grounded = sum(int(m.get("grounded_claims", 0)) for m in meta)
    total = sum(int(m.get("total_claims", 0)) for m in meta)
    lat = [float(m.get("latency_ms", r.latency_ms)) for m, r in zip(meta, rs, strict=True)]
    tokens = [r.output.usage.input_tokens + r.output.usage.output_tokens for r in rs]
    return {
        "trials": len(rs),
        "pass_rate": run.summary.task_success,
        "first_pass_rate": statistics.fmean(bool(m.get("first_passed")) for m in meta)
        if meta
        else 0.0,
        "critic_pass_rate": statistics.fmean(bool(m.get("passed")) for m in meta) if meta else 0.0,
        "grounded_claim_rate": grounded / total if total else 0.0,
        "claims_total": total,
        "hallucination_rate": run.summary.hallucination_rate,
        "judge_mean": statistics.fmean(judge) if judge else 0.0,
        "cost_per_task_usd": run.summary.avg_cost_usd,
        "total_cost_usd": run.summary.total_cost_usd,
        "tokens_per_task": statistics.fmean(tokens) if tokens else 0.0,
        "avg_attempts": statistics.fmean(int(m.get("attempts", 0)) for m in meta) if meta else 0.0,
        "p50_latency_ms": _pct(lat, 0.5),
        "p95_latency_ms": _pct(lat, 0.95),
        "errors": run.summary.failed_trials,
    }
