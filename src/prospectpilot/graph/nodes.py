"""LangGraph nodes. State stays small (ids + counters); everything else lives in Postgres.

Every node is wrapped by `instrument()` (span + Prometheus histogram + per-node timing on the
run row). Per-lead LLM cost and processing time are attributed inside `lead_scope()`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict, TypeVar

from sqlalchemy import select

from prospectpilot.agents import enricher, prospector
from prospectpilot.agents.critic import critique_sequence
from prospectpilot.agents.drafting import invalid_output_critique
from prospectpilot.agents.outbox import cancel_followups, flush_due
from prospectpilot.agents.reply_sim import simulate_reply
from prospectpilot.agents.writer import write_sequence
from prospectpilot.graph.deps import PipelineDeps
from prospectpilot.llm.base import LLMError
from prospectpilot.llm.client import track_usage
from prospectpilot.memory import campaigns as cmp
from prospectpilot.memory import learnings as learnings_mem
from prospectpilot.memory import prompts as prompt_registry
from prospectpilot.memory import repo
from prospectpilot.memory.db import session_scope
from prospectpilot.memory.tables import (
    Campaign,
    Company,
    Draft,
    OutboxMessage,
    Person,
    Prospect,
    Reply,
)
from prospectpilot.models import ICP, CompanyCandidate, PersonCandidate
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import current_trace_id, set_attrs, span
from prospectpilot.verify.scorer import VerifyInput, verify_contact

T = TypeVar("T")


class PipelineState(TypedDict, total=False):
    run_id: str
    campaign_id: str
    prospect_ids: list[int]
    to_write: list[int]
    round: int


NodeFn = Callable[[PipelineState], Awaitable[dict[str, Any]]]


def instrument(name: str, fn: NodeFn) -> NodeFn:
    async def wrapped(state: PipelineState) -> dict[str, Any]:
        key = name if name not in ("writer", "critic") else f"{name}#{state.get('round', 0)}"
        started = time.perf_counter()
        with span(f"graph.node.{name}", **{"pp.run_id": state["run_id"], "pp.node": key}) as s:
            update = await fn(state)
            set_attrs(s, **{"pp.update_keys": ",".join(sorted(update))})
        elapsed = time.perf_counter() - started
        metrics.NODE_DURATION.labels(name).observe(elapsed)
        async with session_scope() as session:
            await cmp.add_timing(session, uuid.UUID(state["run_id"]), key, elapsed * 1000)
        return update

    wrapped.__name__ = name
    return wrapped


@asynccontextmanager
async def lead_scope(
    run_id: str, prospect_ids: list[int], scope: str = "campaign"
) -> AsyncIterator[None]:
    """Attribute LLM cost/time of the enclosed work evenly to the given prospects."""
    started = time.perf_counter()
    with track_usage() as usage:
        yield
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not prospect_ids:
        return
    share = 1.0 / len(prospect_ids)
    async with session_scope() as session:
        for pid in prospect_ids:
            p = await session.get(Prospect, pid)
            if p is not None:
                p.cost_usd = (p.cost_usd or 0.0) + usage.cost_usd * share
                p.processing_ms = (p.processing_ms or 0.0) + elapsed_ms * share
        await repo.record_llm_calls(
            session,
            usage,
            run_id=uuid.UUID(run_id),
            prospect_id=prospect_ids[0] if len(prospect_ids) == 1 else None,
            scope=scope,
            trace_id=current_trace_id(),
        )


async def gather_limited(n: int, coros: list[Coroutine[Any, Any, T]]) -> list[T]:
    sem = asyncio.Semaphore(n)

    async def one(c: Coroutine[Any, Any, T]) -> T:
        async with sem:
            return await c

    return await asyncio.gather(*(one(c) for c in coros))


async def _icp(campaign_id: str) -> ICP:
    async with session_scope() as session:
        c = await session.get(Campaign, uuid.UUID(campaign_id))
        assert c is not None
        return ICP.model_validate(c.icp)


def _pick_people(icp: ICP, people: list[PersonCandidate], limit: int) -> list[PersonCandidate]:
    named = [p for p in people if p.full_name]
    targets = [t.lower() for t in icp.target_titles]

    matching = [p for p in named if any(t in (p.title or "").lower() for t in targets)]
    # fall back to whoever is named only when nobody matches the ICP's target titles
    return (matching or named)[:limit]


def build_nodes(deps: PipelineDeps) -> dict[str, NodeFn]:
    settings = deps.settings

    # ----------------------------------------------------------- prospector
    async def prospector_node(state: PipelineState) -> dict[str, Any]:
        icp = await _icp(state["campaign_id"])
        candidates: list[CompanyCandidate] = await prospector.gather(icp, deps.fetcher)
        ids: list[int] = []
        async with session_scope() as session:
            for cand in candidates:
                company = await repo.upsert_company(session, cand)
                unnamed_emails = [p.email for p in cand.people if p.email and not p.full_name]
                if unnamed_emails:
                    company.extra = {**(company.extra or {}), "published_emails": unnamed_emails}
                people = _pick_people(icp, cand.people, settings.max_people_per_company)
                # store every named contact (their published addresses teach the domain pattern)
                stored = {}
                for p in cand.people:
                    if p.full_name:
                        stored[p.full_name] = await repo.upsert_person(
                            session,
                            company.id,
                            p.full_name,
                            p.title,
                            p.email,
                            cand.source,
                            p.source_url,
                        )
                targets: list[int | None] = [
                    stored[p.full_name].id for p in people if p.full_name
                ] or [None]
                for person_id in targets:
                    pr = await repo.add_prospect(
                        session, uuid.UUID(state["campaign_id"]), company.id, person_id
                    )
                    ids.append(pr.id)
        return {"prospect_ids": list(dict.fromkeys(ids))}

    # ------------------------------------------------------------- enricher
    async def enricher_node(state: PipelineState) -> dict[str, Any]:
        by_company: dict[int, list[int]] = {}
        async with session_scope() as session:
            for pid in state["prospect_ids"]:
                p = await session.get(Prospect, pid)
                if p is not None:
                    by_company.setdefault(p.company_id, []).append(pid)

        async def enrich(company_id: int, pids: list[int]) -> None:
            async with session_scope() as session:
                company = await session.get(Company, company_id)
                assert company is not None
                extractor = f"{deps.llm.provider}:{deps.llm.model_for('extract')}"
                # reuse facts only if they are recent AND came from the same extractor, so a
                # real-model run never silently reuses facts extracted by another model
                fresh = (
                    company.enriched_at is not None
                    and datetime.now(UTC) - company.enriched_at < timedelta(days=7)
                    and (company.extra or {}).get("enriched_by") == extractor
                )
                existing = await repo.company_facts(session, company_id)
                name, domain = company.name, company.domain
            if not (fresh and existing):
                async with lead_scope(state["run_id"], pids):
                    pages = await enricher.collect_pages(deps.fetcher, domain)
                    facts = await enricher.extract_facts(deps.llm, name, pages)
                async with session_scope() as session:
                    if (
                        existing
                    ):  # replace facts from another extractor (cascade keeps drafts' JSON)
                        await repo.delete_facts(session, company_id)
                    await repo.store_facts(session, company_id, facts, deps.embedder)
                    company = await session.get(Company, company_id)
                    assert company is not None
                    company.enriched_at = datetime.now(UTC)
                    company.extra = {**(company.extra or {}), "enriched_by": extractor}
            async with session_scope() as session:
                n = len(await repo.company_facts(session, company_id))
                for pid in pids:
                    p = await session.get(Prospect, pid)
                    assert p is not None
                    p.status = "enriched" if n else "no_facts"

        await gather_limited(deps.concurrency, [enrich(c, p) for c, p in by_company.items()])
        return {}

    # ------------------------------------------------------------- verifier
    async def verifier_node(state: PipelineState) -> dict[str, Any]:
        async def verify(pid: int) -> int | None:
            async with session_scope() as session:
                p = await session.get(Prospect, pid)
                assert p is not None
                company = await session.get(Company, p.company_id)
                assert company is not None
                person = await session.get(Person, p.person_id) if p.person_id else None
                colleagues = await session.scalars(
                    select(Person).where(
                        Person.company_id == company.id,
                        Person.published_email.is_not(None),
                        Person.id != (person.id if person else -1),
                    )
                )
                known = [(c.full_name, c.published_email or "") for c in colleagues]
                published = person.published_email if person else None
                if person is None and (company.extra or {}).get("published_emails"):
                    published = company.extra["published_emails"][0]
                inp = VerifyInput(
                    domain=company.domain,
                    full_name=person.full_name if person else None,
                    published_email=published,
                    known_addresses=known,
                )
                n_facts = len(await repo.company_facts(session, company.id))
            with span("agent.verifier", **{"pp.prospect_id": pid, "pp.domain": inp.domain}) as s:
                started = time.perf_counter()
                result = await verify_contact(inp, deps.resolver)
                set_attrs(s, **{"pp.confidence": result.confidence, "pp.status": result.status})
            async with session_scope() as session:
                p = await session.get(Prospect, pid)
                assert p is not None
                p.email = result.email
                p.email_confidence = result.confidence
                p.verification = result.model_dump(mode="json")
                p.processing_ms = (p.processing_ms or 0.0) + (time.perf_counter() - started) * 1000
                if result.confidence < settings.min_email_confidence or result.email is None:
                    p.status = "unverified"
                elif n_facts < settings.min_facts_per_sequence:
                    p.status = "insufficient_facts"
                else:
                    p.status = "verified"
                    return pid
            return None

        results = await gather_limited(deps.concurrency, [verify(p) for p in state["prospect_ids"]])
        return {"to_write": [p for p in results if p is not None], "round": 0}

    # --------------------------------------------------------------- writer
    async def writer_node(state: PipelineState) -> dict[str, Any]:
        round_ = state.get("round", 0)
        icp = await _icp(state["campaign_id"])
        async with session_scope() as session:
            prompt = await prompt_registry.get_active(session)
            prompt_id, template = prompt.id, prompt.template

        async def write(pid: int) -> None:
            async with session_scope() as session:
                p = await session.get(Prospect, pid)
                assert p is not None
                base = await cmp.prospect_context(session, p)
                query = f"{base.person_title or ''} {base.company_description[:200]}"
                ctx = base.model_copy(
                    update={"learnings": await learnings_mem.search(session, deps.embedder, query)}
                )
                prev = await cmp.latest_draft(session, pid, uuid.UUID(state["run_id"]))
                previous = cmp.draft_sequence(prev) if (prev and round_ > 0) else None
                critique = cmp.draft_critique(prev) if (prev and round_ > 0) else None
            async with lead_scope(state["run_id"], [pid]):
                try:
                    seq = await write_sequence(
                        deps.llm,
                        ctx,
                        icp.offer,
                        icp.sender,
                        template,
                        settings,
                        round_=round_,
                        previous=previous,
                        critique=critique,
                    )
                    seq_json, crit_json = seq.model_dump(), {}
                except LLMError as exc:
                    seq_json, crit_json = None, invalid_output_critique(str(exc)).model_dump()
            async with session_scope() as session:
                session.add(
                    Draft(
                        prospect_id=pid,
                        run_id=uuid.UUID(state["run_id"]),
                        prompt_id=prompt_id,
                        round=round_,
                        sequence=seq_json,
                        critique=crit_json,
                        passed=False,
                    )
                )

        await gather_limited(deps.concurrency, [write(pid) for pid in state.get("to_write", [])])
        return {}

    # --------------------------------------------------------------- critic
    async def critic_node(state: PipelineState) -> dict[str, Any]:
        round_ = state.get("round", 0)
        last_round = round_ >= settings.critic_max_rewrites
        icp = await _icp(state["campaign_id"])

        async def review(pid: int) -> int | None:
            async with session_scope() as session:
                p = await session.get(Prospect, pid)
                assert p is not None
                draft = await cmp.latest_draft(session, pid, uuid.UUID(state["run_id"]))
                assert draft is not None and draft.round == round_
                ctx = await cmp.prospect_context(session, p)
                seq = cmp.draft_sequence(draft)
                draft_id = draft.id
            if seq is not None:
                async with lead_scope(state["run_id"], [pid]):
                    crit = await critique_sequence(
                        deps.llm, seq, ctx, icp.offer, settings, round_=round_
                    )
            else:
                crit = invalid_output_critique("writer returned invalid output")
            async with session_scope() as session:
                d = await session.get(Draft, draft_id)
                p = await session.get(Prospect, pid)
                assert d is not None and p is not None
                d.critique = crit.model_dump()
                d.passed = crit.passed
                d.judge_score = crit.judge.mean if crit.judge else None
                d.grounded_claims = crit.grounded_claims
                d.total_claims = len(crit.claims)
                if crit.passed or last_round:
                    d.is_final = True
                    p.status = "drafted" if crit.passed else "draft_failed"
                    return None
                return pid

        results = await gather_limited(
            deps.concurrency, [review(p) for p in state.get("to_write", [])]
        )
        return {"to_write": [p for p in results if p is not None], "round": round_ + 1}

    # --------------------------------------------------------------- outbox
    async def outbox_node(state: PipelineState) -> dict[str, Any]:
        ready: list[int] = []
        async with session_scope() as session:
            for pid in state["prospect_ids"]:
                p = await session.get(Prospect, pid)
                if p is None or p.status != "drafted":
                    continue
                draft = await cmp.latest_draft(session, pid, uuid.UUID(state["run_id"]))
                seq = cmp.draft_sequence(draft) if draft else None
                if draft is None or seq is None or not draft.passed:
                    continue
                await cmp.schedule_sequence(session, p, draft, seq, settings.followup_days)
                p.status = "scheduled" if deps.mailer is None else "contacted"
                ready.append(pid)
        if deps.mailer is not None and ready:
            await flush_due(deps.mailer, settings, prospect_ids=ready)
        return {}

    # ------------------------------------------------------ reply simulator
    async def reply_sim_node(state: PipelineState) -> dict[str, Any]:
        icp = await _icp(state["campaign_id"])
        if not icp.simulate_replies:
            return {}

        async def sim(pid: int) -> None:
            async with session_scope() as session:
                p = await session.get(Prospect, pid)
                if p is None or p.status not in ("contacted", "scheduled"):
                    return
                draft = await cmp.latest_draft(session, pid, uuid.UUID(state["run_id"]))
                seq = cmp.draft_sequence(draft) if draft else None
                if seq is None:
                    return
                ctx = await cmp.prospect_context(session, p)
                first = await session.scalar(
                    select(OutboxMessage.id).where(
                        OutboxMessage.prospect_id == pid, OutboxMessage.step == 1
                    )
                )
            async with lead_scope(state["run_id"], [pid], scope="reply_sim"):
                reply = await simulate_reply(deps.llm, ctx, seq)
            async with session_scope() as session:
                session.add(
                    Reply(
                        prospect_id=pid,
                        message_id=first,
                        simulated=True,
                        outcome=reply.outcome,
                        probability=reply.probability,
                        body=reply.body,
                    )
                )
                p = await session.get(Prospect, pid)
                assert p is not None
                p.status = f"sim_{reply.outcome}"
            if reply.outcome in ("reply", "objection"):
                await cancel_followups(pid)

        await gather_limited(deps.concurrency, [sim(p) for p in state["prospect_ids"]])
        return {}

    # ------------------------------------------------------------- finalize
    async def finalize_node(state: PipelineState) -> dict[str, Any]:
        async with session_scope() as session:
            for pid in state["prospect_ids"]:
                p = await session.get(Prospect, pid)
                if p is None:
                    continue
                metrics.LEADS.labels(p.status).inc()
                metrics.LEAD_LATENCY.observe((p.processing_ms or 0.0) / 1000)
                metrics.LEAD_COST.observe(p.cost_usd or 0.0)
        return {}

    return {
        "prospector": prospector_node,
        "enricher": enricher_node,
        "verifier": verifier_node,
        "writer": writer_node,
        "critic": critic_node,
        "outbox": outbox_node,
        "reply_simulator": reply_sim_node,
        "finalize": finalize_node,
    }
