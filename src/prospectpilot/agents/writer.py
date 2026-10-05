"""Writer: grounded 3-step sequence with [fact:id] markers, conditioned on retrieved learnings."""

from __future__ import annotations

from prospectpilot.config import Settings
from prospectpilot.llm.base import LLMRequest
from prospectpilot.llm.client import LLM
from prospectpilot.models import Critique, EmailSequence, Offer, ProspectContext, Sender
from prospectpilot.obs.tracing import set_attrs, span


def render_facts(ctx: ProspectContext) -> str:
    lines = []
    for f in ctx.facts:
        date = f" ({f.published_at.isoformat()})" if f.published_at else ""
        lines.append(f"[fact:{f.id}] ({f.kind}{date}) {f.text}  — source: {f.source_url}")
    return "\n".join(lines) or "(no facts)"


def render_writer_prompt(
    ctx: ProspectContext,
    offer: Offer,
    sender: Sender,
    settings: Settings,
    previous: EmailSequence | None = None,
    critique: Critique | None = None,
) -> str:
    person = ctx.person_name or "(name unknown — greet neutrally)"
    title = ctx.person_title or "(title unknown)"
    learnings = "\n".join(f"- {x}" for x in ctx.learnings) or "- (none yet)"
    parts = [
        f"PROSPECT: {person}, {title} at {ctx.company_name} ({ctx.domain})",
        f"COMPANY DESCRIPTION: {ctx.company_description[:400] or '(none)'}",
        f"FACTS (cite with the marker shown):\n{render_facts(ctx)}",
        "OFFER:\n"
        f"- product: {offer.product}\n"
        + "".join(f"- value: {v}\n" for v in offer.value_props)
        + f"- call to action: {offer.call_to_action}",
        f"SENDER: {sender.name}, {sender.title} at {sender.company}",
        f"LEARNINGS FROM PAST CAMPAIGNS (apply where relevant):\n{learnings}",
        f"LIMITS: max {settings.email_max_words} words per email body; "
        f"at least {settings.min_facts_per_sequence} distinct facts across the sequence.",
    ]
    if critique is not None:
        prev = previous.model_dump_json(indent=1) if previous is not None else "(unparseable)"
        parts.append(
            "YOUR PREVIOUS DRAFT FAILED REVIEW. Fix every issue below and rewrite all 3 emails.\n"
            f"ISSUES:\n{critique.feedback}\n"
            f"PREVIOUS DRAFT:\n{prev}"
        )
    return "\n\n".join(parts)


async def write_sequence(
    llm: LLM,
    ctx: ProspectContext,
    offer: Offer,
    sender: Sender,
    system_prompt: str,
    settings: Settings,
    *,
    round_: int = 0,
    previous: EmailSequence | None = None,
    critique: Critique | None = None,
) -> EmailSequence:
    with span(
        "agent.writer",
        **{
            "pp.company": ctx.company_name,
            "pp.round": round_,
            "pp.facts": len(ctx.facts),
            "pp.learnings": len(ctx.learnings),
        },
    ) as s:
        req = LLMRequest(
            purpose="writer",
            system=system_prompt,
            prompt=render_writer_prompt(ctx, offer, sender, settings, previous, critique),
            max_tokens=2000,
            temperature=0.4,
            context={
                "company": ctx.company_name,
                "person_name": ctx.person_name,
                "person_title": ctx.person_title,
                "facts": [f.model_dump(mode="json") for f in ctx.facts],
                "offer": offer.model_dump(),
                "sender": sender.model_dump(),
                "round": round_,
                "critique_codes": critique.failure_codes if critique else [],
                "max_words": settings.email_max_words,
            },
        )
        seq = await llm.complete_json(req, EmailSequence)
        set_attrs(s, **{"pp.fact_ids": ",".join(map(str, sorted(set(seq.fact_ids()))))})
        return seq
