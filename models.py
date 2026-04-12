"""SQLAlchemy ORM + Pydantic schemas for the five MVP tables.

Design decisions:
- ORM models are the source of truth for persistence; Pydantic models are LLM
  structured-output targets and API payload shapes.
- No percentage-of-recovery column anywhere — flat-fee billing only (state bar
  fee-splitting rules; see compliance.py).
- Case.citations stores source char-offsets for every parsed fact so we can
  prove we didn't hallucinate numbers/dates.
- Lead PII lives in pgcrypto-encrypted columns; we never store the KMS key in
  Postgres, the DB is encrypted at rest, and column-level pgcrypto is defense
  in depth until we split PII into a separate schema.
"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    ARRAY,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------- Enums ----------


class CaseStatus(enum.StrEnum):
    parsed = "parsed"
    rejected = "rejected"  # parser decided this isn't a useful class action


class CreativeStatus(enum.StrEnum):
    pending_approval = "pending_approval"
    approved = "approved"
    rejected = "rejected"


class CampaignStatus(enum.StrEnum):
    deploying = "deploying"
    active = "active"
    paused = "paused"
    complete = "complete"
    failed = "failed"


class CreativeModality(enum.StrEnum):
    copy = "copy"
    image = "image"
    video = "video"


# ---------- ORM models ----------


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    pr_newswire_url: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    defendants: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, nullable=False)
    product: Mapped[str | None] = mapped_column(String)
    harm_type: Mapped[str | None] = mapped_column(String)
    class_period_start: Mapped[date | None] = mapped_column(Date)
    class_period_end: Mapped[date | None] = mapped_column(Date)
    geography: Mapped[str | None] = mapped_column(String)
    est_payout_low: Mapped[int | None] = mapped_column(Integer)   # dollars
    est_payout_high: Mapped[int | None] = mapped_column(Integer)  # dollars
    deadline: Mapped[date | None] = mapped_column(Date)
    raw_s3_key: Mapped[str | None] = mapped_column(String)
    citations: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[CaseStatus] = mapped_column(
        Enum(CaseStatus, name="case_status"), default=CaseStatus.parsed, nullable=False, index=True
    )
    reject_reason: Mapped[str | None] = mapped_column(String)
    model_version: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    icps: Mapped[list[ICP]] = relationship(back_populates="case", cascade="all, delete-orphan")
    creatives: Mapped[list[Creative]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )
    campaigns: Mapped[list[Campaign]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )


class ICP(Base):
    __tablename__ = "icps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    demographics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    psychographics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    targeting_hints: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    disqualifiers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    model_version: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    case: Mapped[Case] = relationship(back_populates="icps")
    creatives: Mapped[list[Creative]] = relationship(back_populates="icp")


class Creative(Base):
    __tablename__ = "creatives"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    icp_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("icps.id", ondelete="RESTRICT"), nullable=False
    )
    headline: Mapped[str | None] = mapped_column(String)
    primary_text: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(String)
    image_s3_key: Mapped[str | None] = mapped_column(String)
    video_s3_key: Mapped[str | None] = mapped_column(String)
    arcads_job_id: Mapped[str | None] = mapped_column(String, index=True)
    ai_disclosure_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    policy_check: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    status: Mapped[CreativeStatus] = mapped_column(
        Enum(CreativeStatus, name="creative_status"),
        default=CreativeStatus.pending_approval,
        nullable=False,
        index=True,
    )
    approved_by: Mapped[str | None] = mapped_column(String)  # Slack user id
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    slack_message_ts: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    case: Mapped[Case] = relationship(back_populates="creatives")
    icp: Mapped[ICP] = relationship(back_populates="creatives")
    campaigns: Mapped[list[Campaign]] = relationship(back_populates="creative")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    creative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("creatives.id", ondelete="RESTRICT"), nullable=False
    )
    meta_campaign_id: Mapped[str | None] = mapped_column(String, index=True)
    meta_adset_id: Mapped[str | None] = mapped_column(String)
    meta_ad_id: Mapped[str | None] = mapped_column(String)
    lead_form_id: Mapped[str | None] = mapped_column(String)
    daily_budget_cents: Mapped[int] = mapped_column(Integer, default=5000, nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, name="campaign_status"),
        default=CampaignStatus.deploying,
        nullable=False,
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    case: Mapped[Case] = relationship(back_populates="campaigns")
    creative: Mapped[Creative] = relationship(back_populates="campaigns")
    leads: Mapped[list[Lead]] = relationship(back_populates="campaign")


class Lead(Base):
    """PII-bearing. Name/email/phone are stored pgcrypto-encrypted via the
    migration-time column default. We store the ciphertext as bytea; readers
    must decrypt with pgcrypto.pgp_sym_decrypt() using an app-side KMS-sourced
    key."""

    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("meta_lead_id", name="uq_leads_meta_lead_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("campaigns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    meta_lead_id: Mapped[str] = mapped_column(String, nullable=False)
    # Encrypted PII columns: writers must use pgp_sym_encrypt, readers pgp_sym_decrypt.
    # We type as bytes here; the agent code wraps reads/writes in SQL expressions.
    name_enc: Mapped[bytes | None] = mapped_column()
    email_enc: Mapped[bytes | None] = mapped_column()
    phone_enc: Mapped[bytes | None] = mapped_column()
    intake_answers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    qualification_score: Mapped[int | None] = mapped_column(Integer)
    consent_text_version: Mapped[str] = mapped_column(String, nullable=False)
    consent_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consent_ip: Mapped[str | None] = mapped_column(String)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    campaign: Mapped[Campaign] = relationship(back_populates="leads")


# ---------- Pydantic schemas (LLM structured output targets) ----------


class Citation(BaseModel):
    """A single fact extracted from the source document, with byte offsets that
    allow us to prove the fact was present. Parser MUST return a citation for
    every non-null Case field, or we fail closed."""

    field: str = Field(..., description="Name of the Case field this citation backs")
    quote: str = Field(..., description="Verbatim substring from source doc")
    start_offset: int = Field(..., ge=0)
    end_offset: int = Field(..., ge=0)

    @field_validator("end_offset")
    @classmethod
    def _end_after_start(cls, v: int, info) -> int:
        if "start_offset" in info.data and v <= info.data["start_offset"]:
            raise ValueError("end_offset must be > start_offset")
        return v


class ParsedCase(BaseModel):
    """Claude's structured output for parse_case. Every non-null field must have
    a matching Citation in `citations`, or the ingest agent rejects the result."""

    title: str
    defendants: list[str] = Field(default_factory=list)
    product: str | None = None
    harm_type: str | None = None
    class_period_start: date | None = None
    class_period_end: date | None = None
    geography: str | None = None
    est_payout_low: int | None = None
    est_payout_high: int | None = None
    deadline: date | None = None
    is_viable_class_action: bool = Field(
        ..., description="False if this press release isn't a class action we can run ads for"
    )
    reject_reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)


class GeneratedICP(BaseModel):
    demographics: dict[str, Any] = Field(default_factory=dict)
    psychographics: dict[str, Any] = Field(default_factory=dict)
    targeting_hints: dict[str, Any] = Field(
        default_factory=dict,
        description="Hints for Meta Advantage+; used for creative tone, NOT for narrow targeting parameters (legal ads restriction).",
    )
    disqualifiers: dict[str, Any] = Field(default_factory=dict)


class GeneratedCopy(BaseModel):
    angle: str = Field(
        ...,
        description=(
            "Editorial angle this variant occupies. One of: "
            "'informative', 'empathetic', 'urgent'. Enforced upstream."
        ),
    )
    headline: str
    primary_text: str
    cta: str = Field(..., description="One of: LEARN_MORE, SIGN_UP, APPLY_NOW, GET_QUOTE")
    rationale: str = Field(..., description="Why this copy fits the ICP; for audit logs")


class GeneratedCopyVariants(BaseModel):
    """Claude's structured output when we request N variants in one call.
    The agent enforces len(variants) == expected and angles are distinct."""

    variants: list[GeneratedCopy] = Field(
        ...,
        description="One GeneratedCopy per requested angle, in the order requested.",
    )
