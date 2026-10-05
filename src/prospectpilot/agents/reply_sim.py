"""Reply simulator — every output is SIMULATED and stored with simulated=True.

An LLM plays the prospect persona (built only from stored facts) and predicts reply / objection /
no-reply plus a reply probability, so funnel KPIs can be demonstrated without real outreach.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from prospectpilot.llm.base import LLMRequest
from prospectpilot.llm.client import LLM
from prospectpilot.models import EmailSequence, ProspectContext, strip_markers
from prospectpilot.obs.tracing import set_attrs, span
from prospectpilot.prompts import load as load_prompt

SIMULATED_LABEL = "SIMULATED"


class SimulatedReply(BaseModel):
    outcome: Literal["reply", "objection", "no_reply"]
    probability: float = Field(ge=0.0, le=1.0)
    body: str = ""


async def simulate_reply(llm: LLM, ctx: ProspectContext, seq: EmailSequence) -> SimulatedReply:
    with span(
        "agent.reply_simulator", **{"pp.company": ctx.company_name, "pp.simulated": True}
    ) as s:
        facts = "\n".join(f"- {f.text}" for f in ctx.facts)
        emails = "\n\n".join(
            f"EMAIL {e.step} — {e.subject}\n{strip_markers(e.body)}" for e in seq.emails
        )
        req = LLMRequest(
            purpose="reply_sim",
            system=load_prompt("reply_sim"),
            prompt=(
                f"PERSONA: {ctx.person_name or 'a decision maker'}, "
                f"{ctx.person_title or 'unknown title'} at {ctx.company_name}\n"
                f"FACTS ABOUT YOUR COMPANY:\n{facts}\n\nSEQUENCE YOU RECEIVED:\n{emails}"
            ),
            max_tokens=400,
            temperature=0.7,
            context={
                "persona": {
                    "name": ctx.person_name,
                    "title": ctx.person_title,
                    "company": ctx.company_name,
                },
                "facts_cited": len(set(seq.fact_ids())),
            },
        )
        reply = await llm.complete_json(req, SimulatedReply)
        set_attrs(s, **{"pp.outcome": reply.outcome, "pp.probability": reply.probability})
        return reply
