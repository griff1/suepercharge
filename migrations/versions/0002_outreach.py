"""outreach CRM layer: attorneys, outreach_attempts, email_events, prompt_variants

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    attorney_source = postgresql.ENUM("press_release", "manual", name="attorney_source", create_type=True)
    outreach_stage = postgresql.ENUM(
        "email_sent", "email_opened", "interested", "calendly_sent",
        "call_placed", "call_completed", "follow_up_sent", "dead", "unsubscribed",
        name="outreach_stage", create_type=True,
    )
    email_event_type = postgresql.ENUM(
        "send", "open", "click", "interested", "bounce", "complaint", "unsubscribe",
        name="email_event_type", create_type=True,
    )
    attorney_source.create(op.get_bind(), checkfirst=True)
    outreach_stage.create(op.get_bind(), checkfirst=True)
    email_event_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "attorneys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("firm_name", sa.String, nullable=False),
        sa.Column("contact_name", sa.String),
        sa.Column("contact_email", sa.String),
        sa.Column("contact_phone", sa.String),
        sa.Column("state_code", sa.String(2)),
        sa.Column("source", sa.Enum("press_release", "manual", name="attorney_source", create_type=False), nullable=False, server_default="press_release"),
        sa.Column("outreach_blocked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enriched_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_attorneys_case_id", "attorneys", ["case_id"])
    op.create_index("ix_attorneys_contact_email", "attorneys", ["contact_email"])

    op.create_table(
        "outreach_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("attorney_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("attorneys.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="SET NULL")),
        sa.Column("stage", sa.Enum("email_sent", "email_opened", "interested", "calendly_sent", "call_placed", "call_completed", "follow_up_sent", "dead", "unsubscribed", name="outreach_stage", create_type=False), nullable=False, server_default="email_sent"),
        sa.Column("ses_message_id", sa.String),
        sa.Column("email_subject", sa.String),
        sa.Column("email_variant_key", sa.String),
        sa.Column("call_variant_key", sa.String),
        sa.Column("call_conversation_id", sa.String),
        sa.Column("call_transcript", sa.Text),
        sa.Column("call_summary", sa.Text),
        sa.Column("calendly_link_sent", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.Text),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_outreach_attempts_attorney_id", "outreach_attempts", ["attorney_id"])
    op.create_index("ix_outreach_attempts_lead_id", "outreach_attempts", ["lead_id"])
    op.create_index("ix_outreach_attempts_stage", "outreach_attempts", ["stage"])
    op.create_index("ix_outreach_attempts_ses_message_id", "outreach_attempts", ["ses_message_id"])
    op.create_index("ix_outreach_attempts_call_conversation_id", "outreach_attempts", ["call_conversation_id"])

    op.create_table(
        "email_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("outreach_attempt_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("outreach_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.Enum("send", "open", "click", "interested", "bounce", "complaint", "unsubscribe", name="email_event_type", create_type=False), nullable=False),
        sa.Column("ses_message_id", sa.String),
        sa.Column("user_agent", sa.String),
        sa.Column("ip_address", sa.String),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_email_events_outreach_attempt_id", "email_events", ["outreach_attempt_id"])
    op.create_index("ix_email_events_event_type", "email_events", ["event_type"])

    op.create_table(
        "prompt_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("experiment_key", sa.String, nullable=False),
        sa.Column("variant_id", sa.String, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("weight", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("impressions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("conversions", sa.Integer, nullable=False, server_default="0"),
        sa.Column("parent_variant_id", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("experiment_key", "variant_id", name="uq_variant_experiment_id"),
    )
    op.create_index("ix_prompt_variants_experiment_key", "prompt_variants", ["experiment_key"])
    op.create_index("ix_prompt_variants_is_active", "prompt_variants", ["is_active"])


def downgrade() -> None:
    op.drop_table("prompt_variants")
    op.drop_table("email_events")
    op.drop_table("outreach_attempts")
    op.drop_table("attorneys")
    op.execute("DROP TYPE IF EXISTS email_event_type")
    op.execute("DROP TYPE IF EXISTS outreach_stage")
    op.execute("DROP TYPE IF EXISTS attorney_source")
