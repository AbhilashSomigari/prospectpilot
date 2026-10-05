"""M3 end-to-end: 3 fixture companies through the whole graph in mock-LLM mode.

Needs the compose stack (postgres + mailpit). Offline: recorded sites + static DNS.
"""

from __future__ import annotations

import csv
import json
import uuid
from pathlib import Path

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from prospectpilot.config import REPO_ROOT, get_settings
from prospectpilot.graph.deps import build_deps
from prospectpilot.graph.runner import execute_run, setup_checkpointer
from prospectpilot.memory import campaigns as cmp
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import Draft, FactRow, OutboxMessage, Prospect, Reply, Run
from prospectpilot.models import FACT_MARKER_RE, ICP, Critique
from prospectpilot.obs.tracing import setup_tracing

pytestmark = pytest.mark.integration

COMPANIES = ["Lumen Ledger", "Northwind Robotics", "Quillstack"]


def _mailpit() -> str:
    return get_settings().mailpit_api_url.rstrip("/")


@pytest.fixture
def leads_csv(tmp_path: Path) -> Path:
    src = REPO_ROOT / "examples" / "demo_leads.csv"
    rows = [r for r in csv.DictReader(src.open()) if r["Company"] in COMPANIES]
    out = tmp_path / "leads.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return out


async def test_three_fixture_companies_end_to_end(leads_csv: Path) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.delete(f"{_mailpit()}/api/v1/messages")
    except httpx.HTTPError:
        pytest.skip("mailpit not reachable (run `make up`)")
    await setup_checkpointer()
    spans = InMemorySpanExporter()
    setup_tracing(extra_processors=[SimpleSpanProcessor(spans)])
    icp = ICP.model_validate(
        {
            "name": "e2e",
            "target_titles": ["VP Engineering", "CTO", "Founder"],
            "sources": {"csv_path": str(leads_csv)},
            "offer": {
                "product": "Tracewell",
                "value_props": ["alert engineering teams to failing data pipelines"],
                "call_to_action": "Open to a 15-minute call next week?",
            },
            "simulate_replies": True,
        }
    )
    options = {
        "offline": True,
        "replay_dirs": ["evals/fixtures/sites"],
        "dns_fixture": "evals/fixtures/dns.json",
        "send": True,
    }
    async with session_scope() as s:
        campaign = await cmp.create_campaign(s, icp)
        run = await cmp.create_run(s, campaign.id, options)
        run_id: uuid.UUID = run.id
    deps = build_deps(options)
    try:
        stats = await execute_run(run_id, deps)
    finally:
        await deps.aclose()

    assert stats["prospects"] == 3
    assert stats["drafted"] == 3 and stats["final_pass"] == 3
    assert stats["claims_total"] > 0 and stats["claims_grounded"] == stats["claims_total"]

    async with session_scope() as s:
        r = await s.get(Run, run_id)
        assert r is not None and r.status == "succeeded" and r.trace_id
        for node in ("prospector", "enricher", "verifier", "writer#0", "critic#0", "outbox"):
            assert node in r.node_timings
        prospects = list(
            await s.scalars(select(Prospect).where(Prospect.campaign_id == campaign.id))
        )
        for p in prospects:
            assert (p.email_confidence or 0) >= 0.7
            facts = {
                f.id
                for f in await s.scalars(select(FactRow).where(FactRow.company_id == p.company_id))
            }
            final = await s.scalar(
                select(Draft).where(Draft.prospect_id == p.id, Draft.is_final.is_(True))
            )
            assert final is not None and final.passed
            cited = {int(i) for i in FACT_MARKER_RE.findall(json.dumps(final.sequence))}
            assert len(cited) >= 2 and cited <= facts  # every marker cites this prospect's facts
            assert Critique.model_validate(final.critique).deterministic_passed
            sent = list(
                await s.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.prospect_id == p.id, OutboxMessage.status == "sent"
                    )
                )
            )
            assert [m.step for m in sent] == [1]
            assert all("[fact:" not in m.body for m in sent)  # markers stripped before sending
            assert all(m.smtp_message_id for m in sent)
            scheduled = list(
                await s.scalars(
                    select(OutboxMessage).where(
                        OutboxMessage.prospect_id == p.id, OutboxMessage.step > 1
                    )
                )
            )
            assert len(scheduled) == 2  # follow-ups scheduled (or cancelled on simulated reply)
            reply = await s.scalar(select(Reply).where(Reply.prospect_id == p.id))
            assert reply is not None and reply.simulated is True
        rewritten = await s.scalar(select(Draft).where(Draft.run_id == run_id, Draft.round > 0))
        assert rewritten is not None  # the mock writer's first-draft mistakes forced a rewrite

    names = {sp.name for sp in spans.get_finished_spans()}
    for node in (
        "prospector",
        "enricher",
        "verifier",
        "writer",
        "critic",
        "outbox",
        "reply_simulator",
    ):
        assert f"graph.node.{node}" in names
    assert {"llm.extract", "llm.writer", "llm.critic", "tool.http_fetch", "tool.smtp_send"} <= names
    trace_ids = {
        format(sp.context.trace_id, "032x")
        for sp in spans.get_finished_spans()
        if sp.name.startswith("graph.node.")
    }
    assert trace_ids == {r.trace_id}  # one trace for the whole run

    async with httpx.AsyncClient(timeout=5) as client:
        inbox = (await client.get(f"{_mailpit()}/api/v1/messages")).json()
    assert inbox["total"] == 3
    to = sorted(m["To"][0]["Address"] for m in inbox["messages"])
    assert to == sorted(p.email for p in prospects if p.email)
