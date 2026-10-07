"""Generate RESULTS.md from the database and saved eval artifacts. No hand-written numbers.

Campaign metrics come from one campaign run (default: the latest succeeded run that used a real
model, i.e. provider != mock). Improvement metrics come from the improvement changelog
(`improvement_rounds`, provider != mock) and the raw EvalForge artifacts in evals/runs/.

Usage: uv run python scripts/make_results.py [--run-id UUID] [--out RESULTS.md]
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import statistics
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from prospectpilot.config import REPO_ROOT
from prospectpilot.memory.db import dispose, session_scope
from prospectpilot.memory.tables import (
    Draft,
    ImprovementRound,
    LLMCall,
    Prompt,
    Prospect,
    Reply,
    Run,
)


def pct(n: float, d: float) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[max(0, min(len(v) - 1, round(q * (len(v) - 1))))]


async def _run_metrics(run: Run) -> dict[str, Any]:
    async with session_scope() as s:
        prospects = list(
            await s.scalars(select(Prospect).where(Prospect.campaign_id == run.campaign_id))
        )
        drafts = list(await s.scalars(select(Draft).where(Draft.run_id == run.id)))
        calls = list(await s.scalars(select(LLMCall).where(LLMCall.run_id == run.id)))
        replies = list(
            await s.scalars(
                select(Reply).where(Reply.prospect_id.in_([p.id for p in prospects] or [-1]))
            )
        )
        prompt_ids = {d.prompt_id for d in drafts if d.prompt_id}
        versions = sorted(v for v in [
            (await s.get(Prompt, pid)).version for pid in prompt_ids  # type: ignore[union-attr]
        ])  # fmt: skip
    n = len(prospects)
    verified = sum(1 for p in prospects if (p.email_confidence or 0) >= 0.7)
    first = [d for d in drafts if d.round == 0]
    final = [d for d in drafts if d.is_final]
    grounded = sum(d.grounded_claims for d in final)
    claims = sum(d.total_claims for d in final)
    drafted_ids = {d.prospect_id for d in drafts}
    worked = [p for p in prospects if p.id in drafted_ids]
    lat = [p.processing_ms / 1000 for p in worked]
    costs = [p.cost_usd for p in worked]
    tokens = sum(c.input_tokens + c.output_tokens for c in calls)
    sim = [r for r in replies if r.simulated]
    engaged = sum(1 for r in sim if r.outcome != "no_reply")
    fp = sum(d.passed for d in first)
    fin = sum(d.passed for d in final)
    return {
        "run": f"`{str(run.id)[:8]}` · writer v{','.join(map(str, versions)) or '?'}",
        "Leads processed": str(n),
        "Verified-email confidence ≥ 0.7": f"{verified}/{n} ({pct(verified, n)})",
        "Personalized claims grounded (final drafts)": f"{grounded}/{claims} ({pct(grounded, claims)})",
        "Critic pass — first draft": f"{fp}/{len(first)} ({pct(fp, len(first))})",
        "Critic pass — after rewrite (≤2)": f"{fin}/{len(final)} ({pct(fin, len(final))})",
        "Emails delivered to Mailpit (step 1)": str((run.stats or {}).get("emails_sent")),
        "LLM cost per drafted lead (USD)": f"{statistics.fmean(costs) if costs else 0:.5f}",
        "LLM tokens per drafted lead": f"{tokens / len(worked):.0f}" if worked else "0",
        "Latency per drafted lead p50 / p95 (s)": f"{quantile(lat, 0.5):.1f} / {quantile(lat, 0.95):.1f}",
        "**SIMULATED** reply-or-objection rate": f"{engaged}/{len(sim)} ({pct(engaged, len(sim))})",
        "_meta": run,
    }


async def campaign_section(run_id: uuid.UUID | None) -> list[str]:
    async with session_scope() as s:
        if run_id is not None:
            r = await s.get(Run, run_id)
            runs = [r] if r else []
        else:
            runs = [
                r for r in await s.scalars(
                    select(Run).where(Run.status == "succeeded").order_by(Run.finished_at)
                )
                if (r.stats or {}).get("provider") not in (None, "mock")
            ]  # fmt: skip
    if not runs:
        return ["## Campaign runs", "", "_No succeeded campaign run with a real model yet._", ""]
    cols = [await _run_metrics(r) for r in runs]
    st = runs[-1].stats or {}
    models = st.get("models", {})
    keys = [k for k in cols[0] if k not in ("run", "_meta")]
    lines = [
        "## Campaign runs",
        "",
        f"Provider **{st.get('provider')}** · writer `{models.get('large')}` · critic / extraction / "
        f"reply simulator `{models.get('small')}` · embeddings `{st.get('embedder')}` · offline replay "
        "of 8 fictional fixture companies (`examples/demo_icp.yaml`), facts re-extracted every run.",
        "",
        "| Metric | " + " | ".join(c["run"] for c in cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    lines += [f"| {k} | " + " | ".join(c[k] for c in cols) + " |" for k in keys]
    lines += [
        "",
        "Runs in time order: "
        + "; ".join(
            f"`{c['_meta'].id}` finished {c['_meta'].finished_at:%Y-%m-%d %H:%M} UTC, "
            f"trace `{c['_meta'].trace_id}`"
            for c in cols
        ),
        "",
        "Latency per lead = wall-clock time attributed to that lead across enrichment (shared per",
        "company), verification, drafting and critique. With 8 leads, campaign numbers are small",
        "samples; the eval suite below (40 fixtures × 2 trials) is the statistically stronger signal.",
        "Replies come from the LLM reply simulator and are **SIMULATED**, not real prospect behavior.",
        "",
    ]
    return lines


async def improvement_section() -> list[str]:
    async with session_scope() as s:
        rounds = [r for r in await s.scalars(select(ImprovementRound).order_by(ImprovementRound.round))
                  if r.provider != "mock" and r.status == "completed"]  # fmt: skip
    if not rounds:
        return [
            "## Self-improvement rounds",
            "",
            "_No completed rounds with a real model yet._",
            "",
        ]
    first = rounds[0]
    lines = [
        "## Self-improvement rounds",
        "",
        f"Provider **{first.provider}** · models `{first.models}` · suite: "
        f"{first.metrics_before.get('trials')} trials per evaluation "
        "(frozen fixtures × trials, same seeds for ACTIVE and every candidate).",
        "",
        "Eval pass rate = EvalForge `task_success` = share of trials whose final sequence passed every "
        "grader (JSON contract, cites a known fact, all deterministic critic checks, every claim "
        "grounded, judge ≥ 3.5/5).",
        "",
        "| Round | ACTIVE before | pass rate (ACTIVE) | best candidate | pass rate (best) | judge before → best | grounded claims before → best | tokens/lead before → best | decision |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rounds:
        b, a = r.metrics_before, r.metrics_after
        best = next((c for c in r.candidates if c["metrics"] == a), None)
        lines.append(
            f"| {r.round} | v{_active_version(r)} "
            f"| {b['pass_rate']:.3f} | {('v' + str(best['version']) + ' ' + best['name']) if best else '-'} "
            f"| {a['pass_rate']:.3f} | {b['judge_mean']:.2f} → {a['judge_mean']:.2f} "
            f"| {b['grounded_claim_rate']:.3f} → {a['grounded_claim_rate']:.3f} "
            f"| {b['tokens_per_task']:.0f} → {a['tokens_per_task']:.0f} | **{r.decision}** |"
        )
    series = [rounds[0].metrics_before["pass_rate"]] + [
        (r.metrics_after if r.decision == "promoted" else r.metrics_before)["pass_rate"]
        for r in rounds
    ]
    lines += [
        "",
        "ACTIVE prompt pass rate by round (round 0 = seed prompt; after round k = the prompt that "
        "was ACTIVE once round k finished):",
        "",
        "| " + " | ".join(f"round {i}" for i in range(len(series))) + " |",
        "|" + "---|" * len(series),
        "| " + " | ".join(f"{v:.3f}" for v in series) + " |",
        "",
        "Note: ACTIVE is re-evaluated at the start of every round with the learnings accumulated so "
        "far, so the 'before' of round k+1 can differ from the 'after' of round k (new learnings, "
        "sampling variance).",
        "",
        "Per-round failure clusters (primary failed check on the ACTIVE prompt):",
        "",
    ]
    for r in rounds:
        cl = ", ".join(f"{c['code']}×{c['count']} ({c['stage']})" for c in r.failure_clusters)
        lines.append(f"- round {r.round}: {cl or 'none'} — artifacts `{r.artifacts.get('dir')}`")
    lines.append("")
    lines.append("Gate results for each round's best candidate:")
    lines.append("")
    for r in rounds:
        g = "; ".join(
            f"{x['gate']} {'PASS' if x['passed'] else 'FAIL'} ({x['actual']:.4g} vs {x['threshold']})"
            for x in r.gate_results
        )
        lines.append(f"- round {r.round}: {g}")
    lines.append("")
    return lines


def _trial_successes(path: Path) -> dict[tuple[str, int], bool]:
    data = json.loads(path.read_text())
    return {(r["task_id"], r["trial_index"]): bool(r["success"]) for r in data["results"]}


def paired_bootstrap_ci(
    a: dict[tuple[str, int], bool], b: dict[tuple[str, int], bool], n: int = 5000, seed: int = 7
) -> tuple[float, float, float]:
    """95% CI of pass-rate(b) - pass-rate(a), resampling fixtures (trials of a fixture stay together)."""
    keys = sorted(set(a) & set(b))
    tasks = sorted({k[0] for k in keys})
    by_task = {t: [(a[k], b[k]) for k in keys if k[0] == t] for t in tasks}
    rng = random.Random(seed)

    def diff(sample: list[str]) -> float:
        pairs = [p for t in sample for p in by_task[t]]
        return sum(y - x for x, y in pairs) / len(pairs)

    point = diff(tasks)
    stats = sorted(diff([rng.choice(tasks) for _ in tasks]) for _ in range(n))
    return point, stats[int(0.025 * n)], stats[int(0.975 * n)]


async def interpretation_section() -> list[str]:
    async with session_scope() as s:
        rounds = [r for r in await s.scalars(select(ImprovementRound).order_by(ImprovementRound.round))
                  if r.provider != "mock" and r.status == "completed"]  # fmt: skip
    if not rounds:
        return []
    lines = [
        "## Reading these numbers",
        "",
        "Computed from the per-trial run artifacts (paired by fixture and trial seed; bootstrap over",
        "fixtures, 5,000 resamples). A promotion is only evidence of improvement if its interval",
        "excludes 0.",
        "",
        "| Round | promoted | Δ pass rate vs ACTIVE | trials gained (of N) | paired 95% CI of Δ |",
        "|---|---|---|---|---|",
    ]
    evals_dir = REPO_ROOT / "evals" / "runs"
    for r in rounds:
        d = evals_dir / f"round-{r.round:02d}"
        active = d / f"active-v{_active_version(r)}.json"
        best_v = (r.artifacts or {}).get("best_version")
        best = next(iter(d.glob(f"candidate-v{best_v}-*.json")), None)
        if not active.exists() or best is None:
            continue
        a, b = _trial_successes(active), _trial_successes(best)
        point, lo, hi = paired_bootstrap_ci(a, b)
        gained = round(point * len(a))
        lines.append(
            f"| {r.round} | v{best_v} ({r.decision}) | {point:+.3f} | {gained:+d} of {len(a)} "
            f"| [{lo:+.3f}, {hi:+.3f}] |"
        )
    # winner's curse: the promoted prompt's score in the round that selected it vs its
    # re-evaluation as ACTIVE at the start of the next round
    drift = []
    for prev, nxt in itertools.pairwise(rounds):
        if prev.decision == "promoted":
            drift.append(
                f"v{(prev.artifacts or {}).get('best_version')}: {prev.metrics_after['pass_rate']:.3f} "
                f"when selected (round {prev.round}) → {nxt.metrics_before['pass_rate']:.3f} when "
                f"re-evaluated as ACTIVE (round {nxt.round})"
            )
    if drift:
        lines += ["", "Selected vs re-evaluated pass rate of each promoted prompt:", ""]
        lines += [f"- {x}" for x in drift]
    lines += [
        "",
        "Taking the best of three candidates on the same noisy 80-trial evaluation favours lucky",
        "variants (winner's curse), and each round's re-evaluation also adds that round's new learnings",
        "to the writer's context, so it is not a pure replicate. The promotion gate here is",
        '"strictly better than ACTIVE" with no significance test; see the CI column before reading',
        "the round-by-round series as a trend.",
        "",
    ]
    return lines


def _active_version(r: ImprovementRound) -> str:
    return str((r.artifacts or {}).get("active_version", r.active_prompt_id))


async def build(run_id: str | None) -> str:
    parts = [
        "# RESULTS",
        "",
        f"_Generated by `uv run python scripts/make_results.py` on {datetime.now(UTC):%Y-%m-%d %H:%M} UTC "
        "from the Postgres database and `evals/runs/`. Do not edit by hand._",
        "",
        "All companies and people are fictional (`.test` domains); all email went to the local "
        "Mailpit sandbox; replies are **SIMULATED**.",
        "",
    ]
    parts += await campaign_section(uuid.UUID(run_id) if run_id else None)
    parts += await improvement_section()
    parts += await interpretation_section()
    parts += [
        "## How these numbers were produced",
        "",
        "```bash",
        "make up                                                  # postgres, mailpit, otel, grafana, ...",
        "LLM_PROVIDER=ollama uv run python scripts/build_eval_fixtures.py   # freeze the 47 eval fixtures",
        "scripts/run_real_rounds.sh 3 40 2   # campaign (before) → 3 improve rounds (40 fixtures × 2 trials) → campaign (after)",
        "uv run python scripts/make_results.py",
        "```",
        "",
        "Raw outputs: EvalForge run artifacts per variant, failure analysis, optimizer proposals,",
        "prompt diffs and summaries in `evals/runs/round-NN/`; console logs in `evals/runs/logs/`.",
        "Hardware: Apple M4, 16 GB (local ollama).",
        "",
    ]
    await dispose()
    return "\n".join(parts)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "RESULTS.md")
    a = ap.parse_args()
    a.out.write_text(asyncio.run(build(a.run_id)))
    print(f"wrote {a.out}")
