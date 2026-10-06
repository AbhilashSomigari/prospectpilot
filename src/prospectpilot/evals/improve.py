"""The self-improvement loop (`prospectpilot improve`, POST /improve, nightly EventBridge job).

One round:
 1. evaluate the ACTIVE writer prompt on the EvalForge suite (frozen fixtures, N trials, fixed seeds)
 2. failure analysis: cluster failing trials by root cause → learnings memory
 3. optimizer: 3 candidate prompts targeting the top clusters → prompt registry (candidate)
 4. evaluate every candidate on the same suite, same seeds, same learnings snapshot
 5. promote the best candidate only if it beats ACTIVE on grounded-and-passing rate AND passes the
    regression gates (cost +15% max, judge mean −0.2 max)
 6. append the changelog row (diff, metrics before/after, gates, decision) + raw run artifacts
"""

from __future__ import annotations

import difflib
import json
import logging
import shlex
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from evalforge.models import EvalRun, GateSpec
from evalforge.regression import evaluate_gates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from prospectpilot.config import get_settings
from prospectpilot.evals.suite import run_suite
from prospectpilot.llm.base import LLMError, LLMRequest
from prospectpilot.llm.client import LLM, get_llm
from prospectpilot.llm.embeddings import get_embedder
from prospectpilot.memory import learnings as learnings_mem
from prospectpilot.memory import prompts as registry
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import ImprovementRound
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import set_attrs, span
from prospectpilot.prompts import load as load_prompt

log = logging.getLogger(__name__)

PRIORITY = [
    "invalid_output", "judge_error", "ungrounded_claims", "unknown_fact_ids",
    "missing_fact_markers", "word_limit", "spam_words", "placeholder", "subject",
    "judge_below_threshold",
]  # fmt: skip
COST_REGRESSION = 0.15  # relative
JUDGE_REGRESSION = 0.2  # absolute, on the 1-5 rubric mean


# ----------------------------------------------------------------- queueing
async def queue_round(trials: int = 2, limit: int | None = None, command: str = "") -> int:
    llm = get_llm()
    async with session_scope() as s:
        current = await s.scalar(select(func.max(ImprovementRound.round)))
        row = ImprovementRound(
            round=int(current or 0) + 1,
            status="queued",
            provider=llm.provider,
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


# --------------------------------------------------------- failure analysis
class Cluster(BaseModel):
    code: str
    stage: Literal["final", "first_draft"]
    count: int
    tasks: list[str]
    examples: list[str]


class Finding(BaseModel):
    code: str
    root_cause: str
    learning: str


class Findings(BaseModel):
    findings: list[Finding] = Field(default_factory=list)


def _primary(codes: list[str]) -> str:
    ranked = sorted(codes, key=lambda c: PRIORITY.index(c) if c in PRIORITY else len(PRIORITY))
    return ranked[0] if ranked else "other"


def cluster_failures(run: EvalRun, min_clusters: int = 2) -> list[Cluster]:
    """Group failing trials by their primary failed check (deterministic, explainable)."""

    def build(stage: Literal["final", "first_draft"]) -> list[Cluster]:
        groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for r in run.results:
            m = r.output.metadata
            if stage == "final":
                if r.success:
                    continue
                codes = list(m.get("final_failures") or [])
                if not codes:  # critic passed but an eval grader failed
                    codes = [g.grader for g in r.grades if not g.passed] or ["error"]
            else:
                codes = list(m.get("first_failures") or [])
                if not codes:
                    continue
            detail = str(m.get("feedback") or r.error or "")[:400]
            groups[_primary(codes)].append((r.task_id, detail))
        out = [
            Cluster(code=code, stage=stage, count=len(items),
                    tasks=sorted({t for t, _ in items}),
                    examples=[d for _, d in items if d][:3])
            for code, items in groups.items()
        ]  # fmt: skip
        return sorted(out, key=lambda c: -c.count)

    clusters = build("final")
    if len(clusters) < min_clusters:
        # few final failures: learn from first-draft failures too (they cost rewrites + latency)
        clusters += build("first_draft")
    return clusters[:6]


async def analyze_failures(llm: LLM, clusters: list[Cluster]) -> Findings:
    if not clusters:
        return Findings()
    body = "\n\n".join(
        f"CLUSTER {c.code} ({c.stage}, {c.count} trials)\nEXAMPLE FEEDBACK:\n"
        + "\n".join(f"- {e[:300]}" for e in c.examples)
        for c in clusters
    )
    req = LLMRequest(
        purpose="failure_analysis",
        system=load_prompt("failure_analysis"),
        prompt=body,
        max_tokens=1200,
        temperature=0.0,
        context={"clusters": [c.model_dump() for c in clusters]},
    )
    return await llm.complete_json(req, Findings)


# ---------------------------------------------------------------- optimizer
class Proposal(BaseModel):
    name: str
    rationale: str
    guidance: str


class Proposals(BaseModel):
    candidates: list[Proposal]


class Candidate(BaseModel):
    name: str
    rationale: str
    prompt: str
    guidance: str = ""


OUTPUT_SECTION = "Reply with JSON only"


def compose_prompt(active: str, guidance: str, name: str, round_no: int) -> str:
    """Insert optimizer guidance before the output-format section (which stays byte-identical).

    The optimizer proposes *edits*, not a free-form rewrite, so even a small local model cannot
    break the output contract; guidance from earlier promoted rounds accumulates.
    """
    block = f"Additional guidance (round {round_no}, {name}):\n{guidance.strip()}\n\n"
    idx = active.rfind(OUTPUT_SECTION)
    if idx < 0:
        return active.rstrip() + "\n\n" + block
    return active[:idx] + block + active[idx:]


def valid_candidate(c: Candidate, active: str) -> str | None:
    """Reject candidates that would break the output contract (None = ok)."""
    p = c.prompt
    if p.strip() == active.strip():
        return "identical to active"
    if '"emails"' not in p or "fact:" not in p:
        return "dropped the JSON output contract or the [fact:ID] marker rule"
    if c.guidance and ('"emails"' in c.guidance or "{" in c.guidance):
        return "guidance tries to change the output format"
    if c.guidance and len(c.guidance.split()) > 200:
        return "guidance too long"
    if len(p.split()) > 900:
        return "prompt too long"
    return None


async def propose_candidates(
    llm: LLM, active: str, findings: Findings, clusters: list[Cluster], round_no: int = 0
) -> tuple[list[Candidate], list[dict[str, str]]]:
    counts = {c.code: c.count for c in clusters}
    lines = [
        f"- [{f.code}] x{counts.get(f.code, 0)}: {f.root_cause} → learning: {f.learning}"
        for f in findings.findings
    ]
    examples = [e for c in clusters[:3] for e in c.examples[:2]]
    req = LLMRequest(
        purpose="optimizer",
        system=load_prompt("optimizer"),
        prompt=(
            f"CURRENT PROMPT:\n<<<\n{active}\n>>>\n\nFAILURE FINDINGS:\n" + "\n".join(lines)
            + "\n\nEXAMPLE REVIEWER FEEDBACK:\n" + "\n".join(f"- {e[:300]}" for e in examples)
        ),
        max_tokens=1500,
        temperature=0.7,
        context={"prompt": active, "findings": findings.model_dump()["findings"],
                 "clusters": [c.model_dump() for c in clusters]},
    )  # fmt: skip
    proposed = await llm.complete_json(req, Proposals, repairs=2)
    kept: list[Candidate] = []
    rejected: list[dict[str, str]] = []
    for p in proposed.candidates[:3]:
        c = Candidate(
            name=p.name, rationale=p.rationale, guidance=p.guidance,
            prompt=compose_prompt(active, p.guidance, p.name, round_no),
        )  # fmt: skip
        why = valid_candidate(c, active)
        if why is None:
            kept.append(c)
        else:
            rejected.append({"name": c.name, "reason": why})
    return kept, rejected


# -------------------------------------------------------------------- gates
def promotion_gates(
    active: EvalRun, cand: EvalRun, am: dict[str, Any], cm: dict[str, Any]
) -> list[dict[str, Any]]:
    """EvalForge gates where its model fits, local gates where it doesn't (see CLAUDE.md)."""
    specs = [GateSpec(metric="task_success", op=">", value=active.summary.task_success)]
    if active.summary.avg_cost_usd > 0:
        # relative +15% expressed as EvalForge's absolute max_regression
        specs.append(
            GateSpec(metric="avg_cost_usd", op="<=",
                     max_regression=COST_REGRESSION * active.summary.avg_cost_usd)
        )  # fmt: skip
    out: list[dict[str, Any]] = [
        {"gate": g.metric, "source": "evalforge", "passed": g.passed, "actual": g.actual,
         "threshold": g.threshold, "baseline": g.baseline, "delta": g.delta}
        for g in evaluate_gates(cand, specs, baseline=active)
    ]  # fmt: skip
    if active.summary.avg_cost_usd == 0:
        # unpriced (local) provider: tokens per lead are the honest cost proxy
        limit = (1 + COST_REGRESSION) * am["tokens_per_task"]
        out.append({"gate": "tokens_per_task (cost proxy; provider unpriced)", "source": "local",
                    "passed": cm["tokens_per_task"] <= limit, "actual": cm["tokens_per_task"],
                    "threshold": f"<= {limit:.0f} (+15%)", "baseline": am["tokens_per_task"]})  # fmt: skip
    floor = am["judge_mean"] - JUDGE_REGRESSION
    out.append({"gate": "judge_mean", "source": "local", "passed": cm["judge_mean"] >= floor,
                "actual": cm["judge_mean"], "threshold": f">= {floor:.3f} (-0.2)",
                "baseline": am["judge_mean"]})  # fmt: skip
    return out


def _rank(m: dict[str, Any]) -> tuple[float, float, float]:
    return (m["pass_rate"], m["judge_mean"], -m["tokens_per_task"])


def prompt_diff(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), old_name, new_name
        )
    )


# -------------------------------------------------------------------- round
def _artifact_dir(round_no: int) -> Path:
    d = get_settings().evals_dir / "runs" / f"round-{round_no:02d}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(get_settings().evals_dir.parent))
    except ValueError:
        return str(p)


async def execute_round(round_id: int) -> dict[str, Any]:
    async with session_scope() as s:
        row = await s.get(ImprovementRound, round_id)
        assert row is not None
        round_no = row.round
        trials = int((row.artifacts or {}).get("trials", 2))
        limit = (row.artifacts or {}).get("limit")
        row.status = "running"
        row.started_at = datetime.now(UTC)
    try:
        return await _execute(round_id, round_no, trials, limit)
    except Exception as exc:
        log.exception("improvement round %s failed", round_no)
        async with session_scope() as s:
            row = await s.get(ImprovementRound, round_id)
            if row is not None:
                row.status = "failed"
                row.decision = "error"
                row.finished_at = datetime.now(UTC)
                row.artifacts = {**(row.artifacts or {}), "error": f"{type(exc).__name__}: {exc}"}
        raise


async def _execute(round_id: int, round_no: int, trials: int, limit: int | None) -> dict[str, Any]:
    llm = get_llm()
    embedder = get_embedder()
    out_dir = _artifact_dir(round_no)
    with span("improve.round", **{"pp.round": round_no, "pp.provider": llm.provider,
                                    "pp.trials": trials}) as root:  # fmt: skip
        async with session_scope() as s:
            active = await registry.get_active(s)
            active_id, active_v, active_text = active.id, active.version, active.template
            snapshot = [x.text for x in await learnings_mem.all_active(s)]
            row = await s.get(ImprovementRound, round_id)
            assert row is not None
            row.active_prompt_id = active_id

        # 1. evaluate ACTIVE
        log.info("round %s: evaluating ACTIVE writer v%s", round_no, active_v)
        with span("improve.eval_active", **{"pp.prompt_version": active_v}):
            base_run = await run_suite(active_text, trials=trials, limit=limit, learnings=snapshot,
                                       agent_name=f"writer-v{active_v}",
                                       out=out_dir / f"active-v{active_v}.json", llm=llm)  # fmt: skip
        am = base_run.metadata["metrics"]

        # 2. failure analysis → learnings
        clusters = cluster_failures(base_run)
        try:
            with span("improve.failure_analysis", **{"pp.clusters": len(clusters)}):
                findings = await analyze_failures(llm, clusters)
        except LLMError as exc:
            log.warning("failure analysis failed: %s", exc)
            findings = Findings()
        async with session_scope() as s:
            for f in findings.findings:
                cl = next((x for x in clusters if x.code == f.code), None)
                await learnings_mem.add_learning(
                    s, embedder, f.learning, f.code, round_=round_no,
                    evidence={"root_cause": f.root_cause, "count": cl.count if cl else 0,
                              "tasks": cl.tasks[:10] if cl else []},
                )  # fmt: skip
        (out_dir / "failure_analysis.json").write_text(json.dumps(
            {"clusters": [c.model_dump() for c in clusters], "findings": findings.model_dump()["findings"]},
            indent=1))  # fmt: skip

        # 3. optimizer → candidates
        with span("improve.optimizer"):
            try:
                cands, rejected = await propose_candidates(
                    llm, active_text, findings, clusters, round_no
                )
            except LLMError as exc:
                log.warning("optimizer failed: %s", exc)
                cands, rejected = [], [{"name": "all", "reason": str(exc)[:300]}]
        async with session_scope() as s:
            rows = [
                await registry.add_candidate(s, registry.WRITER, c.prompt, active_id,
                                             f"[{c.name}] {c.rationale}")
                for c in cands
            ]  # fmt: skip
            cand_ids = [(r.id, r.version) for r in rows]
        (out_dir / "optimizer.json").write_text(json.dumps(
            {"candidates": [c.model_dump() for c in cands], "rejected": rejected}, indent=1))  # fmt: skip

        # 4. evaluate candidates (same suite, seeds, learnings snapshot)
        results: list[dict[str, Any]] = []
        runs: dict[int, EvalRun] = {}
        for (pid, ver), c in zip(cand_ids, cands, strict=True):
            log.info("round %s: evaluating candidate v%s (%s)", round_no, ver, c.name)
            with span("improve.eval_candidate", **{"pp.prompt_version": ver, "pp.name": c.name}):
                cr = await run_suite(c.prompt, trials=trials, limit=limit, learnings=snapshot,
                                     agent_name=f"writer-v{ver}-{c.name}",
                                     out=out_dir / f"candidate-v{ver}-{c.name}.json", llm=llm)  # fmt: skip
            runs[pid] = cr
            cm = cr.metadata["metrics"]
            gates = promotion_gates(base_run, cr, am, cm)
            results.append({"prompt_id": pid, "version": ver, "name": c.name, "rationale": c.rationale,
                            "metrics": cm, "gates": gates,
                            "gates_passed": all(g["passed"] for g in gates)})  # fmt: skip
            async with session_scope() as s:
                await registry.set_scores(s, pid, {f"round_{round_no}": cm})
        async with session_scope() as s:
            await registry.set_scores(s, active_id, {f"round_{round_no}": am})

        # 5. promote the best candidate only if it passes every gate
        best = max(results, key=lambda r: _rank(r["metrics"]), default=None)
        decision = "kept_active"
        if best is not None and best["gates_passed"]:
            decision = "promoted"
        async with session_scope() as s:
            if decision == "promoted":
                assert best is not None
                await registry.promote(s, best["prompt_id"])
            await registry.retire_candidates(s, registry.WRITER)

        best_text = next((c.prompt for (pid, _), c in zip(cand_ids, cands, strict=True)
                          if best and pid == best["prompt_id"]), active_text)  # fmt: skip
        diff = prompt_diff(active_text, best_text, f"writer-v{active_v}",
                           f"writer-v{best['version']}" if best else "none")  # fmt: skip
        summary = {
            "round": round_no, "provider": llm.provider, "models": llm.models(), "trials": trials,
            "tasks": len({r.task_id for r in base_run.results}), "decision": decision,
            "active": {"prompt_id": active_id, "version": active_v, "metrics": am},
            "candidates": results, "best": best["version"] if best else None,
            "rejected_candidates": rejected, "learnings_snapshot": snapshot,
            "new_learnings": [f.learning for f in findings.findings],
        }  # fmt: skip
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
        (out_dir / "prompt.diff").write_text(diff)

        # 6. changelog row
        async with session_scope() as s:
            row = await s.get(ImprovementRound, round_id)
            assert row is not None
            row.status = "completed"
            row.decision = decision
            row.winner_prompt_id = best["prompt_id"] if (best and decision == "promoted") else None
            row.prompt_diff = diff
            row.metrics_before = am
            row.metrics_after = best["metrics"] if best else am
            row.candidates = results
            row.gate_results = best["gates"] if best else []
            row.failure_clusters = [c.model_dump() for c in clusters]
            row.finished_at = datetime.now(UTC)
            row.artifacts = {
                **(row.artifacts or {}),
                "dir": _rel(out_dir),
                "active_version": active_v,
                "best_version": best["version"] if best else None,
            }
        for k in ("pass_rate", "judge_mean", "cost_per_task_usd", "p50_latency_ms"):
            metrics.IMPROVE_METRIC.labels(k, "active").set(am[k])
            if best:
                metrics.IMPROVE_METRIC.labels(k, "best_candidate").set(best["metrics"][k])
        set_attrs(root, **{"pp.decision": decision, "pp.active_pass_rate": am["pass_rate"],
                           "pp.best_pass_rate": best["metrics"]["pass_rate"] if best else None})  # fmt: skip
        return summary


async def run_round(trials: int = 2, limit: int | None = None) -> dict[str, Any]:
    command = "prospectpilot " + " ".join(shlex.quote(a) for a in sys.argv[1:])
    round_id = await queue_round(trials=trials, limit=limit, command=command)
    return await execute_round(round_id)


def cluster_counts(clusters: list[dict[str, Any]]) -> Counter[str]:
    return Counter({c["code"]: c["count"] for c in clusters})
