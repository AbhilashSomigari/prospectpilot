"""Generate RESULTS.md from the database and saved eval artifacts. No hand-written numbers.

Campaign metrics come from one campaign run (default: the latest succeeded run that used a real
model, i.e. provider != mock). Improvement metrics come from the improvement changelog
(`improvement_rounds`, provider != mock) and the raw EvalForge artifacts in evals/runs/.

Usage: uv run python scripts/make_results.py [--run-id UUID] [--out RESULTS.md]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from prospectpilot.config import REPO_ROOT
from prospectpilot.memory.db import dispose, session_scope
from prospectpilot.memory.tables import (
    Draft,
    ImprovementRound,
    LLMCall,
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


async def campaign_section(run_id: uuid.UUID | None) -> list[str]:
    async with session_scope() as s:
        if run_id is None:
            runs = list(await s.scalars(
                select(Run).where(Run.status == "succeeded").order_by(Run.finished_at.desc())
            ))  # fmt: skip
            real = [r for r in runs if (r.stats or {}).get("provider") not in (None, "mock")]
            run = real[0] if real else None
        else:
            run = await s.get(Run, run_id)
        if run is None:
            return ["## Campaign run", "", "_No succeeded campaign run with a real model yet._", ""]
        st = run.stats or {}
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
    sim_engaged = sum(1 for r in replies if r.simulated and r.outcome != "no_reply")
    sim_total = sum(1 for r in replies if r.simulated)
    sim_prob = statistics.fmean(r.probability for r in replies if r.simulated) if sim_total else 0.0
    models = st.get("models", {})
    lines = [
        "## Campaign run",
        "",
        f"Run `{run.id}` · provider **{st.get('provider')}** · writer `{models.get('large')}` · "
        f"critic/extraction/reply-sim `{models.get('small')}` · embeddings `{st.get('embedder')}` · "
        f"finished {run.finished_at:%Y-%m-%d %H:%M} UTC · trace `{run.trace_id}`",
        "",
        f"Options: `{st.get('options')}` (offline replay of the fictional fixture companies).",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Leads processed | {n} |",
        f"| Leads with verified-email confidence ≥ 0.7 | {verified}/{n} ({pct(verified, n)}) |",
        f"| Personalized claims grounded (final drafts) | {grounded}/{claims} ({pct(grounded, claims)}) |",
        f"| Critic pass rate — first draft | {sum(d.passed for d in first)}/{len(first)} ({pct(sum(d.passed for d in first), len(first))}) |",
        f"| Critic pass rate — after rewrite (≤2) | {sum(d.passed for d in final)}/{len(final)} ({pct(sum(d.passed for d in final), len(final))}) |",
        f"| Emails delivered to the Mailpit sandbox (step 1) | {st.get('emails_sent')} |",
        f"| LLM cost per drafted lead (USD) | {statistics.fmean(costs) if costs else 0:.5f} |",
        f"| LLM tokens, whole run | {tokens} ({len(calls)} calls) |",
        f"| Latency per drafted lead p50 / p95 (s) | {quantile(lat, 0.5):.1f} / {quantile(lat, 0.95):.1f} |",
        f"| **SIMULATED** reply-or-objection rate | {sim_engaged}/{sim_total} ({pct(sim_engaged, sim_total)}) — mean simulated reply probability {sim_prob:.3f} |",
        "",
        "Latency per lead = wall-clock time attributed to that lead across enrichment (shared per",
        "company), verification, drafting and critique. Replies are produced by the LLM reply",
        "simulator and are **SIMULATED** — they are not real prospect behavior.",
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
    parts += [
        "## How these numbers were produced",
        "",
        "```bash",
        "make up                                              # postgres, mailpit, otel, grafana, ...",
        "LLM_PROVIDER=ollama uv run python scripts/build_eval_fixtures.py   # freeze eval fixtures",
        "LLM_PROVIDER=ollama uv run prospectpilot demo --inline              # campaign run",
        "make improve PROVIDER=ollama   # x3: one self-improvement round each",
        "uv run python scripts/make_results.py",
        "```",
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
