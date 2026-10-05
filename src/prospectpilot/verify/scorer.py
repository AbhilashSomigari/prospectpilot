"""Combine all checks into one verification verdict with a 0-1 confidence and reasons.

Deliberately no SMTP RCPT probing: catch-all status is always reported as "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from prospectpilot.models import EmailCandidate, VerificationResult
from prospectpilot.verify.lists import is_disposable, is_role
from prospectpilot.verify.mx import MXResolver
from prospectpilot.verify.patterns import (
    PATTERN_PRIOR,
    infer_domain_pattern,
    local_parts,
    match_patterns,
    split_name,
)
from prospectpilot.verify.syntax import check_syntax, split

# Score components (documented so the confidence is explainable, not magic).
BASE_PUBLISHED = 0.92  # the address was published by the source for this person
BASE_INFERRED = 0.62  # built from the domain's learned pattern ...
PER_EVIDENCE = 0.08  # ... plus this per supporting known address (max 3)
IMPLICIT_MX_PENALTY = 0.15  # mail accepted via A record only
ROLE_CAP = 0.5  # shared inboxes rarely reach the decision maker
CATCH_ALL_UNKNOWN_CAP = 0.95  # we never probe, so never claim certainty

LIKELY_THRESHOLD = 0.7
RISKY_THRESHOLD = 0.3


@dataclass
class VerifyInput:
    domain: str
    full_name: str | None = None
    published_email: str | None = None
    # (full_name, email) pairs known to be real at this domain (other published contacts)
    known_addresses: list[tuple[str, str]] = field(default_factory=list)


def _status(confidence: float) -> Literal["deliverable_likely", "risky", "undeliverable"]:
    if confidence >= LIKELY_THRESHOLD:
        return "deliverable_likely"
    return "risky" if confidence >= RISKY_THRESHOLD else "undeliverable"


async def verify_contact(inp: VerifyInput, resolver: MXResolver) -> VerificationResult:
    domain = inp.domain.lower()
    reasons: list[str] = []
    candidates: list[EmailCandidate] = []
    pattern_used: str | None = None

    name = split_name(inp.full_name) if inp.full_name else None
    if inp.published_email:
        email = inp.published_email.strip().lower()
        base = BASE_PUBLISHED
        reasons.append("address published by the source")
        if name is not None:
            hits = match_patterns(split(email)[0], name) if "@" in email else []
            pattern_used = hits[0] if hits else None
    elif name is not None:
        learned = infer_domain_pattern(inp.known_addresses)
        parts = local_parts(name)
        if learned is not None:
            pattern_used = learned.pattern
            base = BASE_INFERRED + PER_EVIDENCE * min(learned.evidence, 3)
            reasons.append(
                f"pattern '{learned.pattern}' inferred from {learned.evidence} known "
                f"address(es) at {domain}"
            )
        else:
            pattern_used = max(PATTERN_PRIOR, key=lambda p: PATTERN_PRIOR[p])
            base = PATTERN_PRIOR[pattern_used]
            reasons.append(
                f"no known addresses at {domain}; using most common format '{pattern_used}' "
                "(unconfirmed guess)"
            )
        email = f"{parts[pattern_used]}@{domain}"
        ranked = sorted(PATTERN_PRIOR, key=lambda p: (p != pattern_used, -PATTERN_PRIOR[p]))
        candidates = [
            EmailCandidate(
                email=f"{parts[p]}@{domain}",
                pattern=p,
                score=round(base if p == pattern_used else PATTERN_PRIOR[p] * 0.5, 3),
            )
            for p in ranked[:4]
        ]
    else:
        return VerificationResult(
            email=None,
            confidence=0.0,
            status="no_candidate",
            reasons=["no person name and no published address; nothing to construct"],
        )

    ok, why = check_syntax(email)
    if not ok:
        return VerificationResult(
            email=email, confidence=0.0, status="undeliverable", reasons=[*reasons, why]
        )
    local, email_domain = split(email)

    if is_disposable(email_domain):
        return VerificationResult(
            email=email,
            confidence=0.05,
            status="undeliverable",
            is_disposable=True,
            reasons=[*reasons, "disposable email provider"],
        )

    mx = await resolver.lookup(email_domain)
    confidence = base
    if mx.null_mx:
        return VerificationResult(
            email=email,
            confidence=0.0,
            status="undeliverable",
            reasons=[*reasons, "domain publishes a null MX (accepts no mail)"],
        )
    if not mx.accepts_mail:
        return VerificationResult(
            email=email,
            confidence=0.0,
            status="undeliverable",
            reasons=[*reasons, f"no mail exchanger for {email_domain} ({mx.error})"],
        )
    if mx.implicit:
        confidence -= IMPLICIT_MX_PENALTY
        reasons.append("no MX record; mail would go to the A record (implicit MX)")
    else:
        reasons.append(f"MX ok ({mx.hosts[0]})")

    role = is_role(local)
    if role:
        confidence = min(confidence, ROLE_CAP)
        reasons.append(f"role address '{local}@' (shared inbox)")

    confidence = min(confidence, CATCH_ALL_UNKNOWN_CAP)
    reasons.append("catch-all status unknown (no SMTP probing by design)")
    confidence = round(max(0.0, min(1.0, confidence)), 3)
    return VerificationResult(
        email=email,
        confidence=confidence,
        status=_status(confidence),
        is_role=role,
        mx_hosts=mx.hosts,
        pattern=pattern_used,
        reasons=reasons,
        candidates=candidates,
    )
