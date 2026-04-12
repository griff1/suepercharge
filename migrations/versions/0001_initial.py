"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-12
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute("DO $$ BEGIN CREATE TYPE case_status AS ENUM ('parsed', 'rejected'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE creative_status AS ENUM ('pending_approval', 'approved', 'rejected'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    op.execute("DO $$ BEGIN CREATE TYPE campaign_status AS ENUM ('deploying', 'active', 'paused', 'complete', 'failed'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")

    op.create_table(
        "cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pr_newswire_url", sa.String, nullable=False, unique=True),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("defendants", postgresql.ARRAY(sa.String), nullable=False, server_default="{}"),
        sa.Column("product", sa.String),
        sa.Column("harm_type", sa.String),
        sa.Column("class_period_start", sa.Date),
        sa.Column("class_period_end", sa.Date),
        sa.Column("geography", sa.String),
        sa.Column("est_payout_low", sa.Integer),
        sa.Column("est_payout_high", sa.Integer),
        sa.Column("deadline", sa.Date),
        sa.Column("raw_s3_key", sa.String),
        sa.Column("citations", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "status",
            sa.Enum("parsed", "rejected", name="case_status", create_type=False),
            nullable=False,
            server_default="parsed",
        ),
        sa.Column("reject_reason", sa.String),
        sa.Column("model_version", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_cases_pr_newswire_url", "cases", ["pr_newswire_url"])
    op.create_index("ix_cases_status", "cases", ["status"])

    op.create_table(
        "icps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("demographics", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("psychographics", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("targeting_hints", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("disqualifiers", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("model_version", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_icps_case_id", "icps", ["case_id"])

    op.create_table(
        "creatives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "icp_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("icps.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("headline", sa.String),
        sa.Column("primary_text", sa.Text),
        sa.Column("cta", sa.String),
        sa.Column("image_s3_key", sa.String),
        sa.Column("video_s3_key", sa.String),
        sa.Column("arcads_job_id", sa.String),
        sa.Column("ai_disclosure_applied", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("policy_check", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "status",
            sa.Enum(
                "pending_approval", "approved", "rejected",
                name="creative_status", create_type=False,
            ),
            nullable=False,
            server_default="pending_approval",
        ),
        sa.Column("approved_by", sa.String),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("slack_message_ts", sa.String),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_creatives_case_id", "creatives", ["case_id"])
    op.create_index("ix_creatives_arcads_job_id", "creatives", ["arcads_job_id"])
    op.create_index("ix_creatives_status", "creatives", ["status"])

    op.create_table(
        "campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "creative_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("creatives.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("meta_campaign_id", sa.String),
        sa.Column("meta_adset_id", sa.String),
        sa.Column("meta_ad_id", sa.String),
        sa.Column("lead_form_id", sa.String),
        sa.Column("daily_budget_cents", sa.Integer, nullable=False, server_default="5000"),
        sa.Column(
            "status",
            sa.Enum(
                "deploying", "active", "paused", "complete", "failed",
                name="campaign_status", create_type=False,
            ),
            nullable=False,
            server_default="deploying",
        ),
        sa.Column("last_error", sa.Text),
        sa.Column("deployed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_campaigns_case_id", "campaigns", ["case_id"])
    op.create_index("ix_campaigns_meta_campaign_id", "campaigns", ["meta_campaign_id"])
    op.create_index("ix_campaigns_status", "campaigns", ["status"])

    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("meta_lead_id", sa.String, nullable=False),
        sa.Column("name_enc", sa.LargeBinary),
        sa.Column("email_enc", sa.LargeBinary),
        sa.Column("phone_enc", sa.LargeBinary),
        sa.Column("intake_answers", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("qualification_score", sa.Integer),
        sa.Column("consent_text_version", sa.String, nullable=False),
        sa.Column("consent_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_ip", sa.String),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("meta_lead_id", name="uq_leads_meta_lead_id"),
    )
    op.create_index("ix_leads_campaign_id", "leads", ["campaign_id"])


def downgrade() -> None:
    op.drop_table("leads")
    op.drop_table("campaigns")
    op.drop_table("creatives")
    op.drop_table("icps")
    op.drop_table("cases")
    op.execute("DROP TYPE IF EXISTS campaign_status")
    op.execute("DROP TYPE IF EXISTS creative_status")
    op.execute("DROP TYPE IF EXISTS case_status")
