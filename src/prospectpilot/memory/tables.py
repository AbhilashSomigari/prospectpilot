"""SQLAlchemy 2 ORM schema: prospect memory, learnings memory, prompt registry, run ledger."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBED_DIM = 768
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    icp: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[datetime] = _now()


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    node_timings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20))
    source_url: Mapped[str] = mapped_column(Text)
    signals: Mapped[list[str]] = mapped_column(JSONType, default=list)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now()


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("company_id", "full_name", name="uq_person_company_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    full_name: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    published_email: Mapped[str | None] = mapped_column(String(320))
    source: Mapped[str] = mapped_column(String(20))
    source_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Prospect(Base):
    __tablename__ = "prospects"
    __table_args__ = (
        UniqueConstraint("campaign_id", "company_id", "person_id", name="uq_prospect"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    email: Mapped[str | None] = mapped_column(String(320))
    email_confidence: Mapped[float | None] = mapped_column(Float)
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    processing_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FactRow(Base):
    __tablename__ = "facts"
    __table_args__ = (UniqueConstraint("company_id", "text", name="uq_fact_company_text"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[date | None] = mapped_column(Date)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = _now()


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(
        ForeignKey("prospects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"))
    round: Mapped[int] = mapped_column(Integer, default=0)
    sequence: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    critique: Mapped[dict[str, Any]] = mapped_column(JSONType)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    judge_score: Mapped[float | None] = mapped_column(Float)
    grounded_claims: Mapped[int] = mapped_column(Integer, default=0)
    total_claims: Mapped[int] = mapped_column(Integer, default=0)
    is_final: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _now()


class OutboxMessage(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"))
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id", ondelete="CASCADE"))
    step: Mapped[int] = mapped_column(Integer)
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    smtp_message_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now()


class Reply(Base):
    """Replies. Every row produced by the reply simulator has simulated=True."""

    __tablename__ = "replies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey("prospects.id", ondelete="CASCADE"))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("outbox.id", ondelete="SET NULL"))
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    outcome: Mapped[str] = mapped_column(String(20))  # reply | no_reply | objection
    probability: Mapped[float] = mapped_column(Float)
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now()


class Learning(Base):
    __tablename__ = "learnings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(40), default="failure_analysis")
    round: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now()


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    version: Mapped[int] = mapped_column(Integer)
    template: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="candidate", index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"))
    rationale: Mapped[str] = mapped_column(Text, default="")
    eval_scores: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = _now()


class ImprovementRound(Base):
    """The improvement changelog: one row per `prospectpilot improve` round."""

    __tablename__ = "improvement_rounds"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round: Mapped[int] = mapped_column(Integer, unique=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    provider: Mapped[str] = mapped_column(String(40))
    models: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    command: Mapped[str] = mapped_column(Text, default="")
    active_prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id"))
    winner_prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id"))
    decision: Mapped[str | None] = mapped_column(String(20))  # promoted | kept_active
    prompt_diff: Mapped[str] = mapped_column(Text, default="")
    metrics_before: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    metrics_after: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    gate_results: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    failure_clusters: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LLMCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    prospect_id: Mapped[int | None] = mapped_column(Integer, index=True)
    scope: Mapped[str] = mapped_column(String(60), default="campaign")
    purpose: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = _now()
