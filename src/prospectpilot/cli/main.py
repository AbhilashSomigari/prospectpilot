"""Typer CLI: `prospectpilot ...` — same operations as the API, plus demo/eval/improve."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
import yaml
from rich.console import Console
from rich.table import Table

from prospectpilot.config import REPO_ROOT

T = TypeVar("T")

app = typer.Typer(help="ProspectPilot — a self-improving SDR agent", no_args_is_help=True)
db_app = typer.Typer(help="Database management", no_args_is_help=True)
campaign_app = typer.Typer(help="Campaigns", no_args_is_help=True)
outbox_app = typer.Typer(help="Outbox (Mailpit sandbox only)", no_args_is_help=True)
graph_app = typer.Typer(help="Graph utilities", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(campaign_app, name="campaign")
app.add_typer(outbox_app, name="outbox")
app.add_typer(graph_app, name="graph")
console = Console()

DEMO_OPTIONS: dict[str, Any] = {
    "offline": True,
    "replay_dirs": ["evals/fixtures/sites"],
    "dns_fixture": "evals/fixtures/dns.json",
    "send": True,
}


def _run(coro: Coroutine[Any, Any, T]) -> T:
    from prospectpilot.memory import db
    from prospectpilot.obs.tracing import setup_tracing, shutdown_tracing

    async def wrapped() -> T:
        setup_tracing("prospectpilot-cli")
        try:
            return await coro
        finally:
            shutdown_tracing()
            await db.dispose()

    return asyncio.run(wrapped())


def _load_icp(path: Path) -> Any:
    from prospectpilot.models import ICP

    return ICP.model_validate(yaml.safe_load(path.read_text()))


# ------------------------------------------------------------------------- db
@db_app.command("upgrade")
def db_upgrade() -> None:
    """Apply Alembic migrations and create LangGraph checkpoint tables."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    from prospectpilot.graph.runner import setup_checkpointer

    asyncio.run(setup_checkpointer())
    console.print("[green]database at head (+ langgraph checkpoint tables)[/green]")


# ------------------------------------------------------------------ services
@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the FastAPI server."""
    import uvicorn

    uvicorn.run("prospectpilot.api.app:app", host=host, port=port)


@app.command()
def worker() -> None:
    """Run the background worker (queued runs, improvement rounds, due follow-ups)."""
    from prospectpilot.worker import run_worker

    asyncio.run(run_worker())


# ------------------------------------------------------------------ campaigns
@campaign_app.command("create")
def campaign_create(icp_file: Annotated[Path, typer.Argument(exists=True)]) -> None:
    """Create a campaign from an ICP YAML file."""
    from prospectpilot.memory import campaigns as cmp
    from prospectpilot.memory.db import session_scope

    icp = _load_icp(icp_file)

    async def go() -> str:
        async with session_scope() as s:
            return str((await cmp.create_campaign(s, icp)).id)

    console.print(_run(go()))


@campaign_app.command("show")
def campaign_show(campaign_id: str) -> None:
    from prospectpilot.api.app import get_campaign

    console.print_json(json.dumps(_run(get_campaign(uuid.UUID(campaign_id))), default=str))


async def _execute(
    campaign_id: uuid.UUID,
    options: dict[str, Any],
    *,
    inline: bool,
    i_understand: bool,
    timeout_s: float = 900,
) -> uuid.UUID:
    from prospectpilot.graph.deps import build_deps
    from prospectpilot.graph.runner import execute_run
    from prospectpilot.memory import campaigns as cmp
    from prospectpilot.memory.db import session_scope
    from prospectpilot.memory.tables import Run

    async with session_scope() as s:
        run = await cmp.create_run(s, campaign_id, options)
        run_id = run.id
        if inline:
            run.status = "running"  # keep the worker from claiming it
    if inline:
        deps = build_deps(options, i_understand=i_understand)
        try:
            await execute_run(run_id, deps)
        finally:
            await deps.aclose()
        return run_id
    console.print(f"queued run {run_id}; waiting for the worker…")
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        async with session_scope() as s:
            r = await s.get(Run, run_id)
            assert r is not None
            if r.status in ("succeeded", "failed"):
                return run_id
            if r.status == "queued" and time.monotonic() - started > 20:
                console.print(
                    "[yellow]still queued — is the worker running? (or use --inline)[/yellow]"
                )
        await asyncio.sleep(1.0)
    raise typer.Exit(code=1)


@app.command("run")
def run_campaign(
    campaign_id: str,
    inline: Annotated[
        bool, typer.Option(help="execute in this process instead of the worker")
    ] = False,
    offline: bool = False,
    replay_dir: Annotated[list[str] | None, typer.Option()] = None,
    dns_fixture: str | None = None,
    send: bool = True,
    i_understand: Annotated[
        bool,
        typer.Option("--i-understand", help="required (with ALLOW_REAL_SEND) for non-sandbox SMTP"),
    ] = False,
) -> None:
    """Run a campaign through the full graph."""
    options = {
        "offline": offline,
        "replay_dirs": replay_dir or [],
        "dns_fixture": dns_fixture,
        "send": send,
    }
    run_id = _run(
        _execute(uuid.UUID(campaign_id), options, inline=inline, i_understand=i_understand)
    )
    run_show(str(run_id))


@app.command("run-show")
def run_show(run_id: str) -> None:
    """Show a run: status, per-node timings, stats, trace id."""
    from prospectpilot.api.app import get_run

    console.print_json(json.dumps(_run(get_run(uuid.UUID(run_id))), default=str))


@app.command("prospect")
def prospect_show(prospect_id: int) -> None:
    """Show a prospect: facts, verification, drafts, outbox, (simulated) replies."""
    from prospectpilot.api.app import get_prospect

    console.print_json(json.dumps(_run(get_prospect(prospect_id)), default=str))


# --------------------------------------------------------------------- outbox
@outbox_app.command("flush")
def outbox_flush(
    fast_forward_days: Annotated[int, typer.Option(help="treat 'now' as N days ahead (demo)")] = 0,
    i_understand: Annotated[bool, typer.Option("--i-understand")] = False,
) -> None:
    """Send due messages (follow-ups) to the sandbox."""
    from prospectpilot.agents.mailer import Mailer
    from prospectpilot.agents.outbox import flush_due
    from prospectpilot.config import get_settings

    s = get_settings()
    now = datetime.now(UTC) + timedelta(days=fast_forward_days)
    sent = _run(flush_due(Mailer(s, i_understand=i_understand), s, now=now))
    console.print(f"sent {sent} message(s)")


@graph_app.command("mermaid")
def graph_mermaid() -> None:
    from prospectpilot.graph.build import mermaid

    console.print(mermaid(), markup=False)


# ---------------------------------------------------------------- eval / improve
@app.command("eval")
def eval_cmd(
    trials: Annotated[int, typer.Option(help="trials per task")] = 2,
    limit: Annotated[int | None, typer.Option(help="only the first N fixtures")] = None,
    out: Annotated[Path | None, typer.Option(help="EvalForge run artifact path")] = None,
    prompt_file: Annotated[
        Path | None, typer.Option(help="writer prompt (default: ACTIVE)")
    ] = None,
    baseline: Annotated[Path | None, typer.Option(help="EvalForge run to compare against")] = None,
    concurrency: int = 4,
) -> None:
    """Run the EvalForge suite against the ACTIVE writer prompt; exit 2 if a gate fails."""
    from evalforge.regression import evaluate_gates
    from evalforge.storage import load_run

    from prospectpilot.evals.suite import GATES, run_suite
    from prospectpilot.memory import learnings as learnings_mem
    from prospectpilot.memory import prompts as registry
    from prospectpilot.memory.db import session_scope

    async def go() -> Any:
        if prompt_file is not None:
            prompt, snapshot, label = prompt_file.read_text(), [], prompt_file.name
        else:
            try:
                async with session_scope() as s:
                    active = await registry.get_active(s)
                    prompt, label = active.template, f"writer-v{active.version}"
                    snapshot = [x.text for x in await learnings_mem.all_active(s)]
            except Exception as exc:  # no DB (e.g. a bare CI job): fall back to the seed prompt
                console.print(
                    f"[yellow]prompt registry unavailable ({type(exc).__name__}); using seed prompt[/yellow]"
                )
                from prospectpilot.prompts import load as load_prompt

                prompt, snapshot, label = load_prompt("writer_v1"), [], "writer-seed"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        path = out or REPO_ROOT / "evals" / "runs" / "scratch" / f"eval-{label}-{stamp}.json"
        return await run_suite(
            prompt,
            trials=trials,
            limit=limit,
            learnings=snapshot,
            agent_name=label,
            out=path,
            concurrency=concurrency,
        ), path

    run, path = _run(go())
    m = run.metadata["metrics"]
    t = Table(title=f"EvalForge · {run.suite_name} · {run.agent_name} · {run.metadata['provider']}")
    t.add_column("metric")
    t.add_column("value", justify="right")
    for k in (
        "trials",
        "pass_rate",
        "first_pass_rate",
        "grounded_claim_rate",
        "hallucination_rate",
        "judge_mean",
        "cost_per_task_usd",
        "tokens_per_task",
        "avg_attempts",
        "p50_latency_ms",
        "p95_latency_ms",
        "errors",
    ):
        v = m[k]
        t.add_row(k, f"{v:.4f}" if isinstance(v, float) else str(v))
    console.print(t)
    console.print(f"run artifact: {path}")
    base = load_run(baseline) if baseline else None
    gates = evaluate_gates(run, GATES, base)
    ok = True
    for g in gates:
        ok &= g.passed
        console.print(
            f"gate {g.metric} {g.threshold}: {g.actual:.4f} -> {'PASS' if g.passed else 'FAIL'}"
        )
    if not ok:
        raise typer.Exit(code=2)  # EvalForge convention: failing gate blocks CI


@app.command()
def improve(
    trials: Annotated[int, typer.Option(help="trials per task (same seeds for every variant)")] = 2,
    limit: Annotated[int | None, typer.Option(help="only the first N fixtures")] = None,
) -> None:
    """Run one self-improvement round now (the worker/EventBridge job does the same nightly)."""
    from prospectpilot.evals.improve import run_round

    summary = _run(run_round(trials=trials, limit=limit))
    am = summary["active"]["metrics"]
    t = Table(
        title=f"Improvement round {summary['round']} · {summary['provider']} · decision: {summary['decision']}"
    )
    for col in (
        "variant",
        "pass_rate",
        "first_pass",
        "grounded",
        "judge",
        "tokens/task",
        "cost/task",
        "gates",
    ):
        t.add_column(col)
    t.add_row(
        f"ACTIVE v{summary['active']['version']}",
        f"{am['pass_rate']:.3f}",
        f"{am['first_pass_rate']:.3f}",
        f"{am['grounded_claim_rate']:.3f}",
        f"{am['judge_mean']:.2f}",
        f"{am['tokens_per_task']:.0f}",
        f"{am['cost_per_task_usd']:.5f}",
        "-",
    )
    for c in summary["candidates"]:
        cm = c["metrics"]
        t.add_row(
            f"v{c['version']} {c['name']}",
            f"{cm['pass_rate']:.3f}",
            f"{cm['first_pass_rate']:.3f}",
            f"{cm['grounded_claim_rate']:.3f}",
            f"{cm['judge_mean']:.2f}",
            f"{cm['tokens_per_task']:.0f}",
            f"{cm['cost_per_task_usd']:.5f}",
            "PASS" if c["gates_passed"] else "FAIL",
        )
    console.print(t)
    console.print(f"new learnings: {summary['new_learnings']}")


@app.command()
def improvements() -> None:
    """Show the improvement changelog."""
    from prospectpilot.api.app import improvements as list_rounds

    rows = _run(list_rounds())
    t = Table(title="Improvement changelog")
    for col in (
        "round",
        "status",
        "provider",
        "decision",
        "pass before",
        "pass after",
        "judge before",
        "judge after",
    ):
        t.add_column(col)
    for r in rows:
        b, a = r["metrics_before"] or {}, r["metrics_after"] or {}
        t.add_row(
            str(r["round"]),
            r["status"],
            r["provider"],
            str(r["decision"]),
            f"{b.get('pass_rate', 0):.3f}",
            f"{a.get('pass_rate', 0):.3f}",
            f"{b.get('judge_mean', 0):.2f}",
            f"{a.get('judge_mean', 0):.2f}",
        )
    console.print(t)


# ----------------------------------------------------------------------- demo
@app.command()
def demo(
    icp_file: Path = REPO_ROOT / "examples" / "demo_icp.yaml",
    inline: Annotated[bool, typer.Option(help="run in this process instead of the worker")] = False,
    fast_forward: Annotated[bool, typer.Option(help="also send the follow-ups now")] = True,
    reenrich: Annotated[bool, typer.Option(help="re-extract facts even if cached")] = False,
) -> None:
    """Full campaign on the fictional fixture companies: offline, no API keys needed."""
    from prospectpilot.agents.mailer import Mailer
    from prospectpilot.agents.outbox import flush_due
    from prospectpilot.config import get_settings
    from prospectpilot.memory import campaigns as cmp
    from prospectpilot.memory.db import session_scope
    from prospectpilot.memory.tables import Run

    settings = get_settings()
    icp = _load_icp(icp_file)

    async def go() -> tuple[Run, int]:
        async with session_scope() as s:
            campaign_id = (await cmp.create_campaign(s, icp)).id
        options = {**DEMO_OPTIONS, "reenrich": reenrich}
        run_id = await _execute(campaign_id, options, inline=inline, i_understand=False)
        followups = 0
        if fast_forward:
            later = datetime.now(UTC) + timedelta(days=max(settings.followup_days) + 1)
            followups = await flush_due(Mailer(settings), settings, now=later)
        async with session_scope() as s:
            run = await s.get(Run, run_id)
            assert run is not None
            return run, followups

    run, followups = _run(go())
    console.rule(f"[bold]ProspectPilot demo[/bold] · provider={settings.llm_provider}")
    if run.status != "succeeded":
        console.print(f"[red]run {run.status}: {run.error}[/red]")
        raise typer.Exit(code=1)
    st = run.stats
    t = Table(show_header=False)
    rows = [
        ("Leads processed", st["prospects"]),
        ("Verified confidence >= 0.7", st["verified_ge_0_7"]),
        ("Sequences drafted", st["drafted"]),
        ("Critic pass: first draft / final", f"{st['first_draft_pass']} / {st['final_pass']}"),
        ("Personalized claims grounded", f"{st['claims_grounded']}/{st['claims_total']}"),
        ("Emails delivered to Mailpit (step 1 + follow-ups)", st["emails_sent"] + followups),
        ("SIMULATED replies (reply or objection)", st["simulated_replies"]),
        ("LLM cost (USD)", f"{st['cost_usd']:.4f}"),
        ("Status breakdown", ", ".join(f"{k}={v}" for k, v in sorted(st["by_status"].items()))),
    ]
    for k, v in rows:
        t.add_row(k, str(v))
    console.print(t)
    console.print(f"Node timings (ms): {run.node_timings}")
    console.print(f"Run:     prospectpilot run-show {run.id}")
    if run.trace_id:
        console.print(f"Trace:   http://localhost:16686/trace/{run.trace_id}")
    console.print("Mailpit: http://localhost:8025   Grafana: http://localhost:3000")


if __name__ == "__main__":
    app()
