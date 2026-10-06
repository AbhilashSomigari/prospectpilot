"""Deterministic stand-ins for model calls (mock provider).

They are NOT a model and their outputs are never reported as model results. They exist so the
pipeline, the rewrite loop and the evaluation plumbing run end-to-end without keys or GPUs. The
writer deliberately makes one of a few typical mistakes on the first draft for some prospects
(chosen by a hash of the company name) and fixes them when given a critique, so the critic's
rewrite path is exercised in the demo and the e2e test.
"""

from __future__ import annotations

import re
import zlib
from typing import Any


def _h(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def _first(name: str | None) -> str:
    return name.split()[0] if name else "there"


def _clause(fact_text: str, company: str) -> str:
    text = fact_text.strip().rstrip(".")
    return text if company.lower() in text.lower() else f"{company}: {text}"


def writer(ctx: dict[str, Any]) -> dict[str, Any]:
    company = str(ctx.get("company", "your company"))
    facts: list[dict[str, Any]] = list(ctx.get("facts", []))
    offer: dict[str, Any] = ctx.get("offer", {})
    sender: dict[str, Any] = ctx.get("sender", {})
    product = offer.get("product", "our product")
    values = offer.get("value_props") or ["save your team time"]
    cta = offer.get("call_to_action", "Open to a quick call?")
    hi = f"Hi {_first(ctx.get('person_name'))},"
    sign = f"Best,\n{sender.get('name', 'Alex')}"
    # prefer news/launch facts for the opener, then roles/tech
    order = {"news": 0, "product": 1, "open_role": 2, "tech_stack": 3}
    ranked = sorted(facts, key=lambda f: (order.get(f.get("kind", ""), 9), f["id"]))
    fa = ranked[0] if ranked else None
    fb = ranked[1] if len(ranked) > 1 else None

    def cite(f: dict[str, Any] | None) -> str:
        if f is None:
            return ""
        return f"I noticed this about {company}: {_clause(f['text'], company)} [fact:{f['id']}]."

    mistake = _h(company) % 3 if int(ctx.get("round", 0)) == 0 else -1
    s1 = f"{hi}\n\n{cite(fa)} {product} can {values[0].rstrip('.')}. {cta}\n\n{sign}"
    s2 = (
        f"{hi}\n\nFollowing up with one more thought. {cite(fb)} "
        f"{product} could help {company} {values[-1].rstrip('.')}. Worth a short chat?\n\n{sign}"
    )
    s3 = f"{hi}\n\nLast note from me. If {product} is not a priority right now, no worries. {cta}\n\n{sign}"
    if mistake == 0:  # forgets to cite in step 1
        s1 = f"{hi}\n\n{product} can {values[0].rstrip('.')}. {cta}\n\n{sign}"
    elif mistake == 1:  # spammy phrasing
        s3 = s3.replace("Last note from me.", "Last note from me, results guaranteed!!!")
    return {
        "emails": [
            {"step": 1, "subject": f"Idea for {company}", "body": s1},
            {"step": 2, "subject": f"Re: Idea for {company}", "body": s2},
            {"step": 3, "subject": f"Closing the loop, {company}", "body": s3},
        ]
    }


_SPAM = re.compile(r"guarantee|!!!|act now|limited time", re.I)


def critic(ctx: dict[str, Any]) -> dict[str, Any]:
    seq = ctx.get("sequence", {})
    emails = seq.get("emails", [])
    bodies = [e.get("body", "") for e in emails]
    cited = {int(m) for b in bodies for m in re.findall(r"\[fact:(\d+)\]", b)}
    product = str(ctx.get("offer", {}).get("product", ""))
    max_words = max((len(b.split()) for b in bodies), default=0)
    spam = any(_SPAM.search(b) for b in bodies)
    return {
        "personalization": min(5, 2 + len(cited)),
        "relevance": 4 if product and any(product in b for b in bodies) else 3,
        "clarity": 5 if max_words <= 90 else (4 if max_words <= 120 else 2),
        "tone": 2 if spam else 4,
        "rationale": "mock judge: scores derived from citations, length and phrasing",
    }


def reply_sim(ctx: dict[str, Any]) -> dict[str, Any]:
    persona = ctx.get("persona", {})
    bucket = _h(persona.get("company"), persona.get("name")) % 100
    cited = int(ctx.get("facts_cited", 0))
    probability = round(min(0.6, 0.04 + 0.03 * min(cited, 3)), 3)
    if bucket < 12:
        return {
            "outcome": "reply",
            "probability": probability,
            "body": "Thanks for reaching out, this is timely. Can you send a few times next week?",
        }
    if bucket < 22:
        return {
            "outcome": "objection",
            "probability": probability,
            "body": "Appreciate it, but we already have something in place for this.",
        }
    return {"outcome": "no_reply", "probability": probability, "body": ""}


_ROOT_CAUSES: dict[str, tuple[str, str]] = {
    "ungrounded_claims": (
        "The writer paraphrases facts loosely or adds unsupported specifics next to a marker.",
        "Restate the cited fact's key terms in the same sentence as its [fact:id] marker and never add numbers that are not in the fact.",
    ),
    "missing_fact_markers": (
        "The writer forgets to cite a fact in the first email.",
        "Open step 1 with a sentence built on one fact and its [fact:id] marker, and cite a different fact in step 2.",
    ),
    "spam_words": (
        "Follow-ups drift into hype when trying to create urgency.",
        "Create urgency with a specific, relevant reason instead of hype words like guaranteed or act now.",
    ),
    "word_limit": (
        "Emails try to cover every value proposition at once.",
        "Keep each email to one idea and one ask so it stays well under the word limit.",
    ),
    "judge_below_threshold": (
        "Emails are generic and do not connect the fact to the offer.",
        "Connect the cited fact to one concrete benefit of the offer in the very next sentence.",
    ),
}


def failure_analysis(ctx: dict[str, Any]) -> dict[str, Any]:
    findings = []
    for c in ctx.get("clusters", []):
        cause, learning = _ROOT_CAUSES.get(
            c["code"], ("The writer misses a review rule.", "Re-read every rule before answering.")
        )
        findings.append({"code": c["code"], "root_cause": cause, "learning": learning})
    return {"findings": findings}


def optimizer(ctx: dict[str, Any]) -> dict[str, Any]:
    rules = [f["learning"] for f in ctx.get("findings", [])][:3] or [
        "Cite one fact in step 1 and a different fact in step 2."
    ]
    return {
        "candidates": [
            {
                "name": "targeted",
                "rationale": "rules for the top failure clusters",
                "guidance": "\n".join(f"- {r}" for r in rules),
            },
            {
                "name": "checklist",
                "rationale": "self-check before answering",
                "guidance": "Before answering check: 1) step 1 has a [fact:ID] marker; 2) two distinct "
                "facts are cited; 3) every marked sentence restates its fact; 4) no email "
                "exceeds the word limit; 5) no hype words.",
            },
            {
                "name": "exemplar",
                "rationale": "shows a grounded sentence",
                "guidance": "Good: 'Saw that your team added offline mode for job sites [fact:ID].' "
                "Bad: 'Your team must be growing fast [fact:ID].' (not in the fact). Real ids "
                "come from the FACTS list.",
            },
        ]
    }
