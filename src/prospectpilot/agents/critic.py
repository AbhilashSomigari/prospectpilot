"""Critic: deterministic grounding/format checks + an LLM-judge rubric.

A personalized claim is (a) any sentence carrying a [fact:id] marker, or (b) an unmarked sentence
that asserts something specific about the prospect's company. A claim is grounded only if every
cited id belongs to this prospect, the sentence lexically overlaps the cited fact, and every
number in it appears in the cited facts. Ungrounded claims fail the draft.
"""

from __future__ import annotations

import re

from prospectpilot.config import Settings
from prospectpilot.llm.base import LLMError, LLMRequest
from prospectpilot.llm.client import LLM
from prospectpilot.models import (
    FACT_MARKER_RE,
    CheckResult,
    ClaimCheck,
    Critique,
    EmailSequence,
    Fact,
    JudgeScores,
    Offer,
    ProspectContext,
    strip_markers,
)
from prospectpilot.obs import metrics
from prospectpilot.obs.tracing import set_attrs, span
from prospectpilot.prompts import load as load_prompt

SPAM_PHRASES = (
    "act now",
    "limited time",
    "click here",
    "buy now",
    "no obligation",
    "risk-free",
    "risk free",
    "100% free",
    "for free",
    "guarantee",
    "guaranteed",
    "urgent",
    "winner",
    "congratulations you",
    "cash bonus",
    "double your",
    "once in a lifetime",
    "special promotion",
    "dear friend",
    "!!!",
    "$$$",
    "100%",
)
_PLACEHOLDER = re.compile(
    r"\[(?!fact:\d+\])[A-Z][A-Za-z _]{1,30}\]|\{\{?\s*\w+\s*\}?\}|<[a-z_ ]{3,30}>"
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[a-z0-9][a-z0-9.+#'-]*")
_NUMBER = re.compile(r"\$?\d[\d,.]*\s?(?:%|k|m|bn|x|\+)?", re.I)
_CLAIM_VERBS = re.compile(
    r"\b(launch(?:ed|es|ing)?|raised|announc(?:ed|es|ing)|releas(?:ed|es|ing)|hiring|hired|"
    r"grew|growing|expand(?:ed|ing)|acquir(?:ed|es)|partner(?:ed|s)|uses?|using|built|"
    r"recently|just (?:shipped|launched|raised)|shipped|opened|won|reached)\b",
    re.I,
)
_STOP = frozenset(
    """the a an and or of to in for on with by at from is are was were be been has have had its
    it this that their our we they as into new now more than also about you your i me my us
    just how what when who which can could would will so if but not any all some get
    see saw noticed read came across congrats congratulations""".split()
)


def _words(text: str) -> list[str]:
    return [w.strip(".'-") for w in _WORD.findall(text.lower())]


def content_words(text: str, extra_stop: frozenset[str] = frozenset()) -> set[str]:
    return {w for w in _words(text) if len(w) > 2 and w not in _STOP and w not in extra_stop}


def word_count(text: str) -> int:
    return len(strip_markers(text).split())


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUMBER.finditer(text):
        raw = m.group(0).strip().lower().replace(",", "").rstrip(".")
        digits = re.sub(r"[^\d.]", "", raw)
        if not digits:
            continue
        # small bare integers ("2 quick ideas", "15-minute call") are not factual claims
        if raw == digits and digits.isdigit() and int(digits) <= 15:
            continue
        out.add(digits)
    return out


def check_claims(seq: EmailSequence, ctx: ProspectContext, offer: Offer) -> list[ClaimCheck]:
    facts: dict[int, Fact] = {f.id: f for f in ctx.facts}
    offer_text = " ".join([offer.product, *offer.value_props, offer.call_to_action])
    offer_numbers = _numbers(offer_text)
    company_tokens = frozenset(_words(ctx.company_name))
    claims: list[ClaimCheck] = []
    for email in seq.emails:
        for raw in _SENTENCE.split(email.body):
            sentence = raw.strip()
            if not sentence:
                continue
            ids = [int(i) for i in FACT_MARKER_RE.findall(sentence)]
            text = strip_markers(sentence)
            if ids:
                missing = [i for i in ids if i not in facts]
                if missing:
                    claims.append(
                        ClaimCheck(
                            step=email.step,
                            sentence=text,
                            fact_ids=ids,
                            grounded=False,
                            reason=f"unknown fact ids {missing}",
                        )
                    )
                    continue
                cited = " ".join(facts[i].text for i in ids)
                overlap = content_words(text, company_tokens) & content_words(cited, company_tokens)
                bad_numbers = _numbers(text) - _numbers(cited) - offer_numbers
                if bad_numbers:
                    claims.append(
                        ClaimCheck(
                            step=email.step,
                            sentence=text,
                            fact_ids=ids,
                            grounded=False,
                            reason=f"numbers not in cited facts: {sorted(bad_numbers)}",
                        )
                    )
                elif len(overlap) < 2:
                    claims.append(
                        ClaimCheck(
                            step=email.step,
                            sentence=text,
                            fact_ids=ids,
                            grounded=False,
                            reason="sentence does not restate the cited fact",
                        )
                    )
                else:
                    claims.append(
                        ClaimCheck(
                            step=email.step,
                            sentence=text,
                            fact_ids=ids,
                            grounded=True,
                            reason=f"overlap {sorted(overlap)[:5]}",
                        )
                    )
                continue
            # unmarked: a specific assertion about the prospect's company counts as a claim
            mentions_company = bool(company_tokens & set(_words(text))) or bool(
                re.search(
                    r"\b(your team|your company|you(?:'ve| have) (?:just )?\w+ed)\b", text, re.I
                )
            )
            numbers = _numbers(text) - offer_numbers
            if (mentions_company and _CLAIM_VERBS.search(text)) or numbers:
                reason = (
                    f"unsupported numbers {sorted(numbers)}"
                    if numbers
                    else "company-specific claim without a [fact:id] marker"
                )
                claims.append(
                    ClaimCheck(
                        step=email.step, sentence=text, fact_ids=[], grounded=False, reason=reason
                    )
                )
    return claims


def deterministic_checks(
    seq: EmailSequence, ctx: ProspectContext, offer: Offer, settings: Settings
) -> tuple[list[CheckResult], list[ClaimCheck]]:
    checks: list[CheckResult] = []
    for e in seq.emails:
        n = word_count(e.body)
        checks.append(
            CheckResult(
                code="word_limit",
                step=e.step,
                passed=n <= settings.email_max_words,
                detail=f"step {e.step}: {n} words (max {settings.email_max_words})",
            )
        )
        low = f"{e.subject}\n{e.body}".lower()
        spam = [p for p in SPAM_PHRASES if p in low]
        checks.append(
            CheckResult(
                code="spam_words",
                step=e.step,
                passed=not spam,
                detail=f"step {e.step}: spam phrases {spam}" if spam else "",
            )
        )
        ph = _PLACEHOLDER.findall(f"{e.subject}\n{e.body}")
        checks.append(
            CheckResult(
                code="placeholder",
                step=e.step,
                passed=not ph,
                detail=f"step {e.step}: unfilled placeholders {ph}" if ph else "",
            )
        )
        subj_ok = 0 < len(e.subject) <= 80 and not e.subject.isupper()
        checks.append(
            CheckResult(
                code="subject",
                step=e.step,
                passed=subj_ok,
                detail="" if subj_ok else f"step {e.step}: subject empty, >80 chars or all caps",
            )
        )
    ids = seq.fact_ids()
    distinct = set(ids)
    step1 = seq.emails[0].fact_ids()
    enough = len(distinct) >= settings.min_facts_per_sequence and bool(step1)
    checks.append(
        CheckResult(
            code="missing_fact_markers",
            passed=enough,
            detail=""
            if enough
            else (
                f"{len(distinct)} distinct [fact:id] markers (need "
                f"{settings.min_facts_per_sequence}, incl. one in step 1)"
            ),
        )
    )
    known = {f.id for f in ctx.facts}
    unknown = sorted(distinct - known)
    checks.append(
        CheckResult(
            code="unknown_fact_ids",
            passed=not unknown,
            detail=f"fact ids not belonging to this prospect: {unknown}" if unknown else "",
        )
    )
    claims = check_claims(seq, ctx, offer)
    bad = [c for c in claims if not c.grounded]
    checks.append(
        CheckResult(
            code="ungrounded_claims",
            passed=not bad,
            detail="; ".join(f"step {c.step}: '{c.sentence[:90]}' ({c.reason})" for c in bad[:4]),
        )
    )
    return checks, claims


def _render_for_judge(seq: EmailSequence, ctx: ProspectContext, offer: Offer) -> str:
    facts = "\n".join(f"- {f.text}" for f in ctx.facts)
    emails = "\n\n".join(
        f"STEP {e.step} — Subject: {e.subject}\n{strip_markers(e.body)}" for e in seq.emails
    )
    return (
        f"PROSPECT: {ctx.person_name or 'unknown'}, {ctx.person_title or 'unknown title'} at "
        f"{ctx.company_name}\nKNOWN FACTS:\n{facts}\nOFFER: {offer.product}\n\n"
        f"SEQUENCE:\n{emails}"
    )


async def judge(llm: LLM, seq: EmailSequence, ctx: ProspectContext, offer: Offer) -> JudgeScores:
    req = LLMRequest(
        purpose="critic",
        system=load_prompt("critic_judge"),
        prompt=_render_for_judge(seq, ctx, offer),
        max_tokens=400,
        temperature=0.0,
        context={
            "sequence": seq.model_dump(),
            "facts": [f.model_dump(mode="json") for f in ctx.facts],
            "offer": offer.model_dump(),
        },
    )
    return await llm.complete_json(req, JudgeScores)


def build_feedback(checks: list[CheckResult], j: JudgeScores | None, min_judge: float) -> str:
    lines = [f"- [{c.code}] {c.detail}" for c in checks if not c.passed]
    if j is not None and j.mean < min_judge:
        lines.append(
            f"- [judge] mean {j.mean:.2f} < {min_judge} (personalization {j.personalization}, "
            f"relevance {j.relevance}, clarity {j.clarity}, tone {j.tone}): {j.rationale}"
        )
    return "\n".join(lines)


async def critique_sequence(
    llm: LLM,
    seq: EmailSequence,
    ctx: ProspectContext,
    offer: Offer,
    settings: Settings,
    *,
    round_: int = 0,
    always_judge: bool = False,
) -> Critique:
    with span("agent.critic", **{"pp.company": ctx.company_name, "pp.round": round_}) as s:
        checks, claims = deterministic_checks(seq, ctx, offer, settings)
        det_ok = all(c.passed for c in checks)
        scores: JudgeScores | None = None
        # Cheap checks first: the LLM judge only runs on drafts that already pass every
        # deterministic check (a failing draft is rewritten regardless of its rubric score).
        if det_ok or always_judge:
            try:
                scores = await judge(llm, seq, ctx, offer)
            except LLMError as exc:
                checks.append(CheckResult(code="judge_error", passed=False, detail=str(exc)[:200]))
                det_ok = False
        passed = det_ok and scores is not None and scores.mean >= settings.critic_min_judge
        critique = Critique(
            passed=passed,
            deterministic_passed=det_ok,
            checks=checks,
            claims=claims,
            judge=scores,
            feedback=build_feedback(checks, scores, settings.critic_min_judge),
        )
        metrics.CRITIC_VERDICTS.labels(str(round_), "pass" if passed else "fail").inc()
        set_attrs(
            s,
            **{
                "pp.passed": passed,
                "pp.deterministic_passed": det_ok,
                "pp.judge_mean": scores.mean if scores else None,
                "pp.failures": ",".join(critique.failure_codes),
                "pp.claims_grounded": f"{critique.grounded_claims}/{len(claims)}",
            },
        )
        return critique
