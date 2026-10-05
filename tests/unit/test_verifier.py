from __future__ import annotations

import pytest

from prospectpilot.verify.lists import is_disposable, is_role
from prospectpilot.verify.mx import MXResult, StaticResolver
from prospectpilot.verify.patterns import (
    infer_domain_pattern,
    local_parts,
    match_patterns,
    split_name,
)
from prospectpilot.verify.scorer import VerifyInput, verify_contact
from prospectpilot.verify.syntax import check_syntax

RESOLVER = StaticResolver(
    {
        "acme.io": ["aspmx.l.google.com", "alt1.aspmx.l.google.com"],
        "nomail.io": ["."],
        "mailinator.com": ["mx.mailinator.com"],
    }
)


# --------------------------------------------------------------------- syntax
@pytest.mark.parametrize(
    "email",
    ["maya.chen@acme.io", "m+tag@acme.co.uk", "o'brien@acme.io", "a_b-c@sub.acme.io"],
)
def test_syntax_valid(email: str) -> None:
    assert check_syntax(email)[0]


@pytest.mark.parametrize(
    ("email", "reason"),
    [
        ("maya.chen.acme.io", "exactly one @"),
        ("maya..chen@acme.io", "local part"),
        (".maya@acme.io", "local part"),
        ("maya@acme", "no TLD"),
        ("maya@-acme.io", "label"),
        ("maya@acme.i0", "TLD"),
        ("x" * 65 + "@acme.io", "64"),
        (" maya@acme.io", "whitespace"),
    ],
)
def test_syntax_invalid(email: str, reason: str) -> None:
    ok, why = check_syntax(email)
    assert not ok and reason in why


# ------------------------------------------------------------- lists / roles
def test_disposable_matches_domain_and_subdomain() -> None:
    assert is_disposable("mailinator.com")
    assert is_disposable("eu.mailinator.com")
    assert not is_disposable("acme.io")
    assert not is_disposable("com")


@pytest.mark.parametrize("local", ["info", "sales", "Support", "hello", "jobs2", "sales+eu"])
def test_role_addresses(local: str) -> None:
    assert is_role(local)


def test_personal_addresses_are_not_roles() -> None:
    assert not is_role("maya.chen") and not is_role("mchen")


# ------------------------------------------------------------------- patterns
def test_split_name_handles_accents_honorifics_and_middle_names() -> None:
    assert split_name("Dr. José María García") == split_name("jose garcia")
    n = split_name("Maya Q. Chen")
    assert n is not None and (n.first, n.last) == ("maya", "chen")
    assert split_name("Prince") is None


def test_local_parts_cover_common_formats() -> None:
    n = split_name("Maya Chen")
    assert n is not None
    lp = local_parts(n)
    assert lp["first.last"] == "maya.chen"
    assert lp["flast"] == "mchen"
    assert lp["f.last"] == "m.chen"
    assert lp["firstl"] == "mayac"
    assert match_patterns("mchen", n) == ["flast"]


def test_infer_domain_pattern_by_votes() -> None:
    known = [("Sam Okafor", "sokafor@acme.io"), ("Lee Park", "lpark@acme.io"),
             ("Ana Lima", "ana.lima@acme.io")]  # fmt: skip
    dp = infer_domain_pattern(known)
    assert dp is not None and dp.pattern == "flast" and dp.evidence == 2 and dp.total_known == 3
    assert infer_domain_pattern([("Nobody", "x@acme.io")]) is None


# --------------------------------------------------------------------- scorer
async def test_published_address_scores_high() -> None:
    r = await verify_contact(
        VerifyInput("acme.io", "Maya Chen", published_email="maya.chen@acme.io"), RESOLVER
    )
    assert r.status == "deliverable_likely" and r.confidence >= 0.9
    assert r.catch_all == "unknown"
    assert r.pattern == "first.last"
    assert any("no SMTP probing" in x for x in r.reasons)


async def test_inferred_pattern_scales_with_evidence() -> None:
    one = await verify_contact(
        VerifyInput("acme.io", "Maya Chen", known_addresses=[("Sam Okafor", "sokafor@acme.io")]),
        RESOLVER,
    )
    three = await verify_contact(
        VerifyInput("acme.io", "Maya Chen", known_addresses=[
            ("Sam Okafor", "sokafor@acme.io"), ("Lee Park", "lpark@acme.io"),
            ("Ana Lima", "alima@acme.io")]),
        RESOLVER,
    )  # fmt: skip
    assert one.email == three.email == "mchen@acme.io"
    assert 0.7 <= one.confidence < three.confidence <= 0.95
    assert one.candidates[0].email == "mchen@acme.io"


async def test_unconfirmed_guess_is_risky() -> None:
    r = await verify_contact(VerifyInput("acme.io", "Maya Chen"), RESOLVER)
    assert r.email == "maya.chen@acme.io"
    assert r.status == "risky" and r.confidence < 0.7
    assert len(r.candidates) == 4


async def test_no_mx_is_undeliverable() -> None:
    r = await verify_contact(VerifyInput("unknown-domain.io", "Maya Chen"), RESOLVER)
    assert r.status == "undeliverable" and r.confidence == 0.0


async def test_null_mx_is_undeliverable() -> None:
    r = await verify_contact(VerifyInput("nomail.io", "Maya Chen"), RESOLVER)
    assert r.status == "undeliverable" and "null MX" in r.reasons[-1]


async def test_disposable_is_rejected() -> None:
    r = await verify_contact(
        VerifyInput("mailinator.com", "Maya Chen", published_email="maya@mailinator.com"),
        RESOLVER,
    )
    assert r.is_disposable and r.status == "undeliverable"


async def test_role_address_is_capped() -> None:
    r = await verify_contact(
        VerifyInput("acme.io", None, published_email="sales@acme.io"), RESOLVER
    )
    assert r.is_role and r.confidence == 0.5 and r.status == "risky"


async def test_implicit_mx_penalty() -> None:
    class ImplicitResolver:
        async def lookup(self, domain: str) -> MXResult:
            return MXResult(domain, [domain], implicit=True)

    r = await verify_contact(
        VerifyInput("acme.io", "Maya Chen", published_email="maya.chen@acme.io"),
        ImplicitResolver(),
    )
    assert r.confidence == pytest.approx(0.77)
    assert any("implicit MX" in x for x in r.reasons)


async def test_no_name_no_email_has_no_candidate() -> None:
    r = await verify_contact(VerifyInput("acme.io"), RESOLVER)
    assert r.status == "no_candidate" and r.email is None
