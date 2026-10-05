"""Pydantic v2 domain models shared by agents, graph, API and evals."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FactKind = Literal["product", "news", "tech_stack", "open_role", "team_size", "funding", "other"]
SourceName = Literal["hn", "github", "website", "csv", "fixture"]

FACT_MARKER_RE = re.compile(r"\[fact:(\d+)\]")


# --------------------------------------------------------------------------- ICP


class TeamSize(BaseModel):
    min: int = 1
    max: int = 100_000


class HNSourceConfig(BaseModel):
    enabled: bool = False
    thread_id: int | None = None  # None = latest "Ask HN: Who is hiring?" thread
    max_posts: int = 200


class GitHubSourceConfig(BaseModel):
    enabled: bool = False
    orgs: list[str] = Field(default_factory=list)
    search_query: str | None = None  # e.g. "type:org location:berlin"
    max_orgs: int = 20


class SourcesConfig(BaseModel):
    hn: HNSourceConfig = Field(default_factory=HNSourceConfig)
    github: GitHubSourceConfig = Field(default_factory=GitHubSourceConfig)
    websites: list[str] = Field(default_factory=list)
    csv_path: str | None = None


class Offer(BaseModel):
    """What the sender is selling. The writer may only make claims about the offer from here."""

    product: str
    value_props: list[str] = Field(default_factory=list)
    call_to_action: str = "Open to a 15-minute call next week?"


class Sender(BaseModel):
    name: str = "Alex Rivera"
    title: str = "Account Executive"
    company: str = "ProspectPilot Demo Co."


class ICP(BaseModel):
    name: str
    industry_keywords: list[str] = Field(default_factory=list)
    team_size: TeamSize = Field(default_factory=TeamSize)
    hiring_signals: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    target_titles: list[str] = Field(default_factory=lambda: ["CTO", "VP Engineering", "Founder"])
    max_companies: int = 25
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    offer: Offer
    sender: Sender = Field(default_factory=Sender)
    simulate_replies: bool = False


# ---------------------------------------------------------------------- sources


class PersonCandidate(BaseModel):
    full_name: str | None = None
    title: str | None = None
    email: str | None = None  # only when the source published it
    source_url: str | None = None


class CompanyCandidate(BaseModel):
    name: str
    domain: str
    source: SourceName
    source_url: str
    description: str = ""
    signals: list[str] = Field(default_factory=list)
    people: list[PersonCandidate] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    team_size: int | None = None

    @field_validator("domain")
    @classmethod
    def _norm_domain(cls, v: str) -> str:
        v = v.strip().lower()
        v = re.sub(r"^https?://", "", v)
        v = v.split("/")[0]
        return v.removeprefix("www.")


# -------------------------------------------------------------------------- facts


class ExtractedFact(BaseModel):
    kind: FactKind
    text: str = Field(min_length=8, max_length=400)
    source_url: str
    published_at: date | None = None


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


class Fact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    text: str
    source_url: str
    fetched_at: datetime | None = None
    published_at: date | None = None


# ------------------------------------------------------------------ verification


class EmailCandidate(BaseModel):
    email: str
    pattern: str
    score: float


class VerificationResult(BaseModel):
    email: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["deliverable_likely", "risky", "undeliverable", "no_candidate"]
    catch_all: Literal["unknown"] = "unknown"  # we never RCPT-probe, so this is always unknown
    is_role: bool = False
    is_disposable: bool = False
    mx_hosts: list[str] = Field(default_factory=list)
    pattern: str | None = None
    reasons: list[str] = Field(default_factory=list)
    candidates: list[EmailCandidate] = Field(default_factory=list)


# ------------------------------------------------------------------ writer/critic


class EmailDraft(BaseModel):
    step: int = Field(ge=1, le=3)
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1)

    def fact_ids(self) -> list[int]:
        return [int(m) for m in FACT_MARKER_RE.findall(self.subject + "\n" + self.body)]


class EmailSequence(BaseModel):
    emails: list[EmailDraft]

    @model_validator(mode="after")
    def _three_steps(self) -> EmailSequence:
        if len(self.emails) != 3:
            raise ValueError(f"sequence must have exactly 3 emails, got {len(self.emails)}")
        self.emails = sorted(self.emails, key=lambda e: e.step)
        if [e.step for e in self.emails] != [1, 2, 3]:
            raise ValueError("emails must have steps 1, 2, 3")
        return self

    def fact_ids(self) -> list[int]:
        return [fid for e in self.emails for fid in e.fact_ids()]


class CheckResult(BaseModel):
    code: str
    passed: bool
    detail: str = ""
    step: int | None = None


class JudgeScores(BaseModel):
    personalization: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)
    rationale: str = ""

    @property
    def mean(self) -> float:
        return (self.personalization + self.relevance + self.clarity + self.tone) / 4


class ClaimCheck(BaseModel):
    step: int
    sentence: str
    fact_ids: list[int]
    grounded: bool
    reason: str = ""


class Critique(BaseModel):
    passed: bool
    deterministic_passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    claims: list[ClaimCheck] = Field(default_factory=list)
    judge: JudgeScores | None = None
    feedback: str = ""

    @property
    def failure_codes(self) -> list[str]:
        codes = [c.code for c in self.checks if not c.passed]
        if self.judge is not None and not self.passed and self.deterministic_passed:
            codes.append("judge_below_threshold")
        return sorted(set(codes))

    @property
    def grounded_claims(self) -> int:
        return sum(1 for c in self.claims if c.grounded)


class ProspectContext(BaseModel):
    """Everything the writer/critic need about one prospect, independent of storage."""

    prospect_id: int | None = None
    company_name: str
    domain: str
    company_description: str = ""
    person_name: str | None = None
    person_title: str | None = None
    facts: list[Fact]
    learnings: list[str] = Field(default_factory=list)


class DraftAttempt(BaseModel):
    round: int
    sequence: EmailSequence | None
    critique: Critique
    raw_error: str | None = None


class DraftOutcome(BaseModel):
    attempts: list[DraftAttempt]

    @property
    def final(self) -> DraftAttempt:
        return self.attempts[-1]

    @property
    def passed(self) -> bool:
        return self.final.critique.passed

    @property
    def first_passed(self) -> bool:
        return self.attempts[0].critique.passed


def strip_markers(text: str) -> str:
    """Remove [fact:id] markers (and the stray whitespace they leave) before sending."""
    out = FACT_MARKER_RE.sub("", text)
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()
