from __future__ import annotations

from pathlib import Path

import pytest

from prospectpilot.agents.critic import check_claims, critique_sequence, deterministic_checks
from prospectpilot.agents.drafting import draft_with_critique
from prospectpilot.llm.client import LLM, track_usage
from prospectpilot.models import EmailSequence, Fact, Offer, ProspectContext, Sender
from prospectpilot.prompts import load as load_prompt

from .helpers import make_settings

OFFER = Offer(
    product="Tracewell",
    value_props=["catch data incidents before stakeholders do", "cut on-call noise"],
    call_to_action="Open to a 15-minute call next week?",
)
CTX = ProspectContext(
    company_name="Acme Analytics",
    domain="acme-analytics.io",
    person_name="Maya Chen",
    person_title="VP Engineering",
    facts=[
        Fact(
            id=11,
            kind="news",
            text="Acme Analytics launched Pipelines 2.0 with column-level lineage on 2026-08-12.",
            source_url="https://acme-analytics.io/blog/p2",
        ),
        Fact(
            id=12,
            kind="open_role",
            text="Acme Analytics is hiring a Senior Platform Engineer.",
            source_url="https://acme-analytics.io/careers",
        ),
        Fact(
            id=13,
            kind="team_size",
            text="Acme Analytics is a team of 45 people across the US and Europe.",
            source_url="https://acme-analytics.io/about",
        ),
    ],
)


def seq(*bodies: str, subject: str = "Quick idea") -> EmailSequence:
    return EmailSequence.model_validate(
        {"emails": [{"step": i + 1, "subject": subject, "body": b} for i, b in enumerate(bodies)]}
    )


GOOD = seq(
    "Hi Maya, congrats on launching Pipelines 2.0 with column-level lineage [fact:11]. "
    "Tracewell helps teams catch data incidents before stakeholders do. Open to a 15-minute call next week?",
    "Hi Maya, I saw you are hiring a Senior Platform Engineer [fact:12]. Tracewell can cut on-call noise for that team.",
    "Hi Maya, last note from me. Open to a 15-minute call next week? Best, Alex",
)


def codes(s: EmailSequence) -> set[str]:
    checks, _ = deterministic_checks(s, CTX, OFFER, make_settings(Path("/tmp")))
    return {c.code for c in checks if not c.passed}


def test_good_sequence_passes_all_checks() -> None:
    assert codes(GOOD) == set()
    claims = check_claims(GOOD, CTX, OFFER)
    assert [c.fact_ids for c in claims] == [[11], [12]]
    assert all(c.grounded for c in claims)


def test_word_limit() -> None:
    long = " ".join(["word"] * 121)
    assert "word_limit" in codes(seq(GOOD.emails[0].body, GOOD.emails[1].body, long))


def test_markers_do_not_count_toward_word_limit() -> None:
    body = " ".join(["word"] * 118) + " [fact:11] [fact:12] [fact:13]"
    assert "word_limit" not in codes(seq(GOOD.emails[0].body, GOOD.emails[1].body, body))


def test_spam_words() -> None:
    assert "spam_words" in codes(
        seq(GOOD.emails[0].body, GOOD.emails[1].body, "Act now, results guaranteed!!!")
    )


def test_missing_fact_markers_requires_two_facts_and_step_one() -> None:
    one_fact = seq(GOOD.emails[0].body, "Hi Maya, following up.", "Bye.")
    assert "missing_fact_markers" in codes(one_fact)
    no_step1 = seq(
        "Hi Maya, quick idea.",
        GOOD.emails[1].body,
        "Pipelines 2.0 with column-level lineage looked great [fact:11].",
    )
    assert "missing_fact_markers" in codes(no_step1)


def test_fact_ids_must_belong_to_prospect() -> None:
    s = seq(GOOD.emails[0].body, "Loved your post on Kubernetes migrations [fact:999].", "Bye.")
    assert {"unknown_fact_ids", "ungrounded_claims"} <= codes(s)


def test_marker_on_unrelated_sentence_is_ungrounded() -> None:
    s = seq(
        "Hi Maya, your new office in Lisbon looks amazing [fact:11].", GOOD.emails[1].body, "Bye."
    )
    assert "ungrounded_claims" in codes(s)


def test_invented_number_is_ungrounded() -> None:
    s = seq(
        "Pipelines 2.0 with column-level lineage cut incidents by 40% [fact:11].",
        GOOD.emails[1].body,
        "Bye.",
    )
    c = check_claims(s, CTX, OFFER)[0]
    assert not c.grounded and "40" in c.reason


def test_unmarked_company_claim_is_ungrounded() -> None:
    s = seq(
        GOOD.emails[0].body,
        GOOD.emails[1].body,
        "Acme Analytics recently raised a big round, congrats!",
    )
    assert "ungrounded_claims" in codes(s)


def test_offer_numbers_are_allowed() -> None:
    assert codes(GOOD) == set()  # "15-minute" comes from the offer CTA


def test_placeholders() -> None:
    s = seq(
        GOOD.emails[0].body.replace("Maya", "[First Name]"), GOOD.emails[1].body, "Hi {first_name}"
    )
    assert "placeholder" in codes(s)


async def test_critique_with_mock_judge_passes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    crit = await critique_sequence(LLM(settings, provider="mock"), GOOD, CTX, OFFER, settings)
    assert crit.passed and crit.judge is not None and crit.judge.mean >= 3.5
    assert crit.grounded_claims == 2


@pytest.mark.parametrize(
    "company", ["Acme Analytics", "Northwind Robotics", "Quillstack", "Lumina Data"]
)
async def test_mock_draft_loop_converges(tmp_path: Path, company: str) -> None:
    settings = make_settings(tmp_path)
    ctx = CTX.model_copy(
        update={
            "company_name": company,
            "facts": [
                f.model_copy(update={"text": f.text.replace("Acme Analytics", company)})
                for f in CTX.facts
            ],
        }
    )
    with track_usage() as usage:
        outcome = await draft_with_critique(
            LLM(settings, provider="mock"), ctx, OFFER, Sender(), load_prompt("writer_v1"), settings
        )
    assert outcome.passed
    assert 1 <= len(outcome.attempts) <= 3
    assert usage.purposes.count("writer") == len(outcome.attempts)
    if not outcome.first_passed:
        assert outcome.attempts[0].critique.failure_codes  # the rewrite was driven by a critique
