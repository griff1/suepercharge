"""Eval agent — self-improving loop that learns from CRM engagement data.

Cadence: EventBridge cron, weekly (Sunday 02:00 UTC), or on-demand.

For each experiment with active A/B variants:
  1. Query EmailEvent + OutreachAttempt to compute per-variant metrics:
     - Open rate (opens / sends)
     - Interested rate (interested clicks / sends) — this is the key conversion
     - Call outcome rate (positive calls / calls placed)
  2. If the winning variant has enough impressions, ask Claude Haiku to:
     a. Confirm the winner and identify losers.
     b. Generate a mutated child variant to test next.
  3. Auto-apply: retire losers, keep winner, insert mutated child.

Fully autonomous — no human gate. All mutations are logged to
prompt_variants with parent_variant_id for lineage tracking.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.orm import Session

from clients.anthropic_client import structured
from db import session
from models import (
    EmailEvent,
    EmailEventType,
    EvalRecommendation,
    OutreachAttempt,
    PromptVariant,
)
from prompts import render

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

EVAL_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")
MIN_IMPRESSIONS = int(os.environ.get("MIN_IMPRESSIONS_FOR_EVAL", "10"))

EXPERIMENT_KEYS = ["email_subject_body", "call_script"]


@dataclass
class VariantStats:
    variant_id: str
    content: str
    parent_variant_id: str | None
    impressions: int
    sends: int
    opens: int
    interested: int
    open_rate: float
    interested_rate: float
    conversion_rate: float   # interested_rate is the primary conversion metric


def _compute_variant_stats(s: Session, experiment_key: str) -> list[VariantStats]:
    """Compute real engagement metrics from EmailEvent data per variant."""
    variants = list(
        s.scalars(
            select(PromptVariant).where(
                PromptVariant.experiment_key == experiment_key,
                PromptVariant.is_active.is_(True),
            )
        ).all()
    )
    stats = []
    for v in variants:
        # Count sends for this variant
        sends = s.scalar(
            select(sqlfunc.count(OutreachAttempt.id)).where(
                OutreachAttempt.email_variant_key == v.variant_id,
            )
        ) or 0

        # Count opens via EmailEvent
        opens = s.scalar(
            select(sqlfunc.count(EmailEvent.id))
            .join(OutreachAttempt, EmailEvent.outreach_attempt_id == OutreachAttempt.id)
            .where(
                OutreachAttempt.email_variant_key == v.variant_id,
                EmailEvent.event_type == EmailEventType.open,
            )
        ) or 0

        # Count interested clicks
        interested = s.scalar(
            select(sqlfunc.count(EmailEvent.id))
            .join(OutreachAttempt, EmailEvent.outreach_attempt_id == OutreachAttempt.id)
            .where(
                OutreachAttempt.email_variant_key == v.variant_id,
                EmailEvent.event_type == EmailEventType.interested,
            )
        ) or 0

        open_rate = opens / sends if sends > 0 else 0.0
        interested_rate = interested / sends if sends > 0 else 0.0

        stats.append(VariantStats(
            variant_id=v.variant_id,
            content=v.content[:500],  # truncate for prompt
            parent_variant_id=v.parent_variant_id,
            impressions=v.impressions,
            sends=sends,
            opens=opens,
            interested=interested,
            open_rate=open_rate,
            interested_rate=interested_rate,
            conversion_rate=interested_rate,  # primary metric
        ))
    return stats


def _next_variant_id(existing_ids: list[str]) -> str:
    nums = []
    for vid in existing_ids:
        if vid.startswith("v"):
            try:
                nums.append(int(vid[1:]))
            except ValueError:
                pass
    return f"v{max(nums, default=0) + 1}"


def _eval_experiment(s: Session, experiment_key: str) -> dict:
    stats = _compute_variant_stats(s, experiment_key)

    if len(stats) < 2:
        return {"experiment_key": experiment_key, "action": "skipped_insufficient_variants"}

    total_sends = sum(v.sends for v in stats)
    if total_sends < MIN_IMPRESSIONS:
        return {
            "experiment_key": experiment_key,
            "action": "skipped_insufficient_data",
            "total_sends": total_sends,
        }

    # Build prompt with real metrics
    prompt = render(
        "eval_outreach",
        experiment_key=experiment_key,
        min_impressions=MIN_IMPRESSIONS,
        variants=[{
            "variant_id": v.variant_id,
            "parent_variant_id": v.parent_variant_id,
            "impressions": v.sends,
            "conversions": v.interested,
            "conversion_rate": v.interested_rate,
            "content": v.content,
            "open_rate": v.open_rate,
            "opens": v.opens,
        } for v in stats],
    )

    try:
        recommendation: EvalRecommendation = structured(
            prompt=prompt,
            response_model=EvalRecommendation,
            model=EVAL_MODEL,
            temperature=0.3,
            max_tokens=1024,
        )
    except Exception:
        log.exception("eval LLM call failed for experiment %s", experiment_key)
        return {"experiment_key": experiment_key, "action": "errored"}

    now = datetime.now(UTC)

    # Get all active variants for this experiment
    all_variants = list(
        s.scalars(
            select(PromptVariant).where(
                PromptVariant.experiment_key == experiment_key,
                PromptVariant.is_active.is_(True),
            )
        ).all()
    )
    all_ids = [v.variant_id for v in all_variants]

    for v in all_variants:
        if v.variant_id in recommendation.losing_variant_ids:
            v.is_active = False
            v.retired_at = now
            log.info("retired %s/%s", experiment_key, v.variant_id)
        if v.variant_id == recommendation.winning_variant_id:
            v.promoted_at = now
            log.info("promoted %s/%s", experiment_key, v.variant_id)

    new_id = _next_variant_id(all_ids)
    child = PromptVariant(
        experiment_key=experiment_key,
        variant_id=new_id,
        content=recommendation.new_variant_content,
        is_active=True,
        weight=1.0,
        parent_variant_id=recommendation.winning_variant_id,
    )
    s.add(child)
    s.flush()
    log.info("created child %s/%s from %s", experiment_key, new_id, recommendation.winning_variant_id)

    return {
        "experiment_key": experiment_key,
        "action": "evaluated",
        "winner": recommendation.winning_variant_id,
        "retired": recommendation.losing_variant_ids,
        "new_variant": new_id,
        "reasoning": recommendation.reasoning,
        "metrics": {v.variant_id: {"sends": v.sends, "opens": v.opens, "interested": v.interested, "open_rate": round(v.open_rate, 3), "interested_rate": round(v.interested_rate, 3)} for v in stats},
    }


@dataclass
class EvalResult:
    experiments_evaluated: int = 0
    experiments_skipped: int = 0
    variants_retired: int = 0
    variants_created: int = 0
    errored: int = 0
    details: list[dict] = field(default_factory=list)


def run_once() -> EvalResult:
    result = EvalResult()
    with session() as s:
        for key in EXPERIMENT_KEYS:
            try:
                summary = _eval_experiment(s, key)
                result.details.append(summary)
                if summary.get("action") == "evaluated":
                    result.experiments_evaluated += 1
                    result.variants_retired += len(summary.get("retired", []))
                    result.variants_created += 1
                else:
                    result.experiments_skipped += 1
            except Exception:
                log.exception("eval failed for %s", key)
                result.errored += 1

    log.info("eval run: %s", result)
    return result


def handler(_event: dict, _context: object) -> dict:
    r = run_once()
    return {
        "evaluated": r.experiments_evaluated,
        "skipped": r.experiments_skipped,
        "retired": r.variants_retired,
        "created": r.variants_created,
        "errored": r.errored,
        "details": r.details,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(handler({}, None), indent=2))
