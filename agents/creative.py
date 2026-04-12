"""Creative agent — generates copy + image + video and runs them through a
Slack approval gate before handing to the campaign agent.

Cadence: EventBridge cron, every 5 minutes (see infra/terraform).

Each invocation runs three idempotent passes:

  1. poll_approvals()  — for pending_approval Creatives with a slack_message_ts,
     fetch reactions; flip status to approved/rejected when a human reacts.
  2. poll_videos()     — for Creatives whose Arcads job was submitted but not
     yet downloaded, check the job status and pull the MP4 into S3.
  3. generate_new()    — for each Case without any Creative, generate copy
     (retry-until-compliant), image, submit the async Arcads job, and post
     to Slack with the image attached (the video link is added in pass 2 via
     a follow-up message).

Keeping the passes separate means a partial failure in one doesn't block the
others, and each pass is safe to re-run.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import compliance
import storage
from clients import (
    arcads_client,
    ideogram_client,
    slack_client,
)
from clients.anthropic_client import structured
from db import session
from models import (
    ICP,
    Case,
    CaseStatus,
    Creative,
    CreativeStatus,
    GeneratedCopy,
    GeneratedCopyVariants,
)
from prompts import render

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

COPY_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
MAX_COPY_RETRIES = 3

# Fixed angle set. Three is the MVP count — small enough for a reviewer to
# eyeball, diverse enough to A/B. Override per-case later once we have
# performance data. Each angle drives both copy tone AND image composition
# (see prompts/image_prompt.j2 and prompts/write_copy_variants.j2).
AD_ANGLES: list[dict[str, str]] = [
    {
        "name": "informative",
        "description": (
            "Factual, calm, low-pressure. Lead with what happened and who "
            "might be eligible. Suited to high-context audiences and complex "
            "products (e.g. securities fraud, product defects)."
        ),
    },
    {
        "name": "empathetic",
        "description": (
            "Acknowledges the harm and the reader's situation. Warm, "
            "low-pressure tone. Suited to sensitive cases — data breach, "
            "medical device, privacy violations."
        ),
    },
    {
        "name": "urgent",
        "description": (
            "Deadline-forward. Emphasizes the claim window closing without "
            "fear-mongering. Suited to cases with a near-term filing deadline."
        ),
    },
]
ANGLE_NAMES: tuple[str, ...] = tuple(a["name"] for a in AD_ANGLES)


# ---------- Copy generation with compliance-retry loop ----------


@dataclass
class CopyResult:
    """Single-variant copy result. Kept for backward compat with the
    single-copy path (try_copy.py, existing tests)."""

    copy: GeneratedCopy
    attempts: int
    findings_history: list[list[compliance.PolicyFinding]]


@dataclass
class VariantsResult:
    variants: list[GeneratedCopy]
    attempts: int
    findings_history: list[list[compliance.PolicyFinding]]


def _scan_variants(variants: list[GeneratedCopy]) -> list[compliance.PolicyFinding]:
    """Return all blocklist findings across every variant."""
    findings: list[compliance.PolicyFinding] = []
    for v in variants:
        findings.extend(compliance.scan_copy(f"{v.headline}\n{v.primary_text}"))
    return findings


def generate_compliant_variants(
    *, case: Case, icp: ICP, angles: list[dict[str, str]] | None = None
) -> VariantsResult | None:
    """Generate one copy per angle in a single Claude call, retry if any
    variant trips the blocklist. Returns None after MAX_COPY_RETRIES."""
    angles = angles or AD_ANGLES
    expected_names = [a["name"] for a in angles]
    history: list[list[compliance.PolicyFinding]] = []
    last_reason: str | None = None

    for attempt in range(1, MAX_COPY_RETRIES + 1):
        prompt = render(
            "write_copy_variants",
            case=case,
            icp=icp,
            angles=angles,
            blocklist=compliance.blocklist_for_prompt(),
            retry_reason=last_reason,
        )
        result = structured(
            prompt=prompt,
            response_model=GeneratedCopyVariants,
            model=COPY_MODEL,
            temperature=0.8,  # higher than single-copy — we want diversity
            max_tokens=2048,
        )
        variants = result.variants

        # Structural checks: right count, right angles, in the order we asked.
        if len(variants) != len(expected_names):
            last_reason = (
                f"expected {len(expected_names)} variants, got {len(variants)}"
            )
            log.warning("copy attempt %d: %s", attempt, last_reason)
            continue
        returned_angles = [v.angle for v in variants]
        if returned_angles != expected_names:
            last_reason = (
                f"angle mismatch: expected {expected_names}, got {returned_angles}"
            )
            log.warning("copy attempt %d: %s", attempt, last_reason)
            continue

        findings = _scan_variants(variants)
        history.append(findings)
        if not findings:
            return VariantsResult(
                variants=variants, attempts=attempt, findings_history=history
            )
        last_reason = "; ".join(sorted({f.pattern for f in findings}))
        log.warning("copy attempt %d violated blocklist: %s", attempt, last_reason)

    log.error("gave up on compliant variants after %d attempts", MAX_COPY_RETRIES)
    return None


def generate_compliant_copy(*, case: Case, icp: ICP) -> CopyResult | None:
    """Single-variant path (compat). Generate copy, re-prompt with the
    blocklist reason on failure. Returns None after MAX_COPY_RETRIES."""
    history: list[list[compliance.PolicyFinding]] = []
    last_reason: str | None = None

    for attempt in range(1, MAX_COPY_RETRIES + 1):
        prompt = render(
            "write_copy",
            case=case,
            icp=icp,
            blocklist=compliance.blocklist_for_prompt(),
            retry_reason=last_reason,
        )
        copy = structured(
            prompt=prompt,
            response_model=GeneratedCopy,
            model=COPY_MODEL,
            temperature=0.7,
            max_tokens=1024,
        )
        combined = f"{copy.headline}\n{copy.primary_text}"
        findings = compliance.scan_copy(combined)
        history.append(findings)
        if not findings:
            return CopyResult(copy=copy, attempts=attempt, findings_history=history)
        last_reason = "; ".join(sorted({f.pattern for f in findings}))
        log.warning(
            "copy attempt %d violated blocklist: %s", attempt, last_reason
        )

    log.error("gave up on compliant copy after %d attempts", MAX_COPY_RETRIES)
    return None


# ---------- Pass 3: generate_new ----------


def _cases_without_creative(s: Session) -> list[tuple[Case, ICP]]:
    """Cases that are parsed, not rejected, and have no Creative yet. Join the
    most recent ICP per case."""
    # One ICP per case in the MVP; pick the first.
    rows = s.execute(
        select(Case, ICP)
        .join(ICP, ICP.case_id == Case.id)
        .where(Case.status == CaseStatus.parsed)
        .where(~Case.creatives.any())
    ).all()
    return [(c, i) for (c, i) in rows]


def _make_creative_row(
    *, case: Case, icp: ICP, copy: GeneratedCopy, copy_attempts: int
) -> tuple[Creative, str | None]:
    """Build a Creative row for one variant: copy + image.

    Video is skipped in MVP (see README §"Deferred"). To re-enable, uncomment
    the Arcads block below and the poll_videos call in run_once. The DB
    columns (video_s3_key, arcads_job_id, ai_disclosure_applied) are preserved
    so re-enabling requires no migration.
    """
    # Image — angle-aware prompt.
    image_prompt = render("image_prompt", case=case, icp=icp, angle=copy.angle)
    image_key: str | None = None
    try:
        image_bytes = ideogram_client.generate(image_prompt, aspect_ratio="1x1")
        image_key = f"creatives/{uuid.uuid4()}.png"
        storage.put_bytes(image_key, image_bytes, "image/png")
    except Exception:
        log.exception("image generation failed for case %s angle=%s", case.id, copy.angle)

    # --- Video (deferred) ---
    # video_script = f"{copy.headline}\n\n{copy.primary_text}"
    # arcads_job_id: str | None = None
    # try:
    #     arcads_job_id = arcads_client.submit_video(
    #         script=video_script, watermark_text=compliance.AI_DISCLOSURE_TEXT,
    #     )
    # except Exception:
    #     log.exception("arcads submit failed for case %s angle=%s", case.id, copy.angle)

    cr = Creative(
        case_id=case.id,
        icp_id=icp.id,
        headline=copy.headline,
        primary_text=copy.primary_text,
        cta=copy.cta,
        image_s3_key=image_key,
        video_s3_key=None,
        arcads_job_id=None,
        # ai_disclosure_applied tracks the video-watermark step; with no video
        # it's moot. The campaign deploy gate only fires when video_s3_key is
        # set, so False here does not block image-only deploys.
        ai_disclosure_applied=False,
        policy_check={
            "copy_attempts": copy_attempts,
            "angle": copy.angle,
            "rationale": copy.rationale,
        },
        status=CreativeStatus.pending_approval,
    )
    return cr, image_key


def _post_for_approval(cr: Creative, case: Case, image_key: str | None) -> None:
    """Post a Slack approval message for the creative. Populates
    cr.slack_message_ts on success."""
    try:
        image_url = storage.presign(image_key) if image_key else None
        ts = slack_client.post_creative_for_approval(
            headline=f"[{(cr.policy_check or {}).get('angle', '?')}] {cr.headline}",
            primary_text=cr.primary_text or "",
            cta=cr.cta or "LEARN_MORE",
            image_url=image_url,
            video_url=None,
            case_title=case.title,
            case_url=case.pr_newswire_url,
        )
        cr.slack_message_ts = ts
    except Exception:
        log.exception("slack post failed for creative %s", cr.id)


def _generate_variants(s: Session, case: Case, icp: ICP) -> list[Creative]:
    """Generate AD_ANGLES-many variants for one case. One Claude call produces
    all copies; image + video run per variant."""
    variants_result = generate_compliant_variants(case=case, icp=icp)
    if variants_result is None:
        # Record a single rejected placeholder so we don't re-try the whole case.
        placeholder = Creative(
            case_id=case.id,
            icp_id=icp.id,
            status=CreativeStatus.rejected,
            policy_check={
                "reason": "copy_blocklist_failed",
                "attempts": MAX_COPY_RETRIES,
            },
        )
        s.add(placeholder)
        return []

    created: list[Creative] = []
    for copy in variants_result.variants:
        cr, image_key = _make_creative_row(
            case=case, icp=icp, copy=copy, copy_attempts=variants_result.attempts
        )
        s.add(cr)
        s.flush()
        _post_for_approval(cr, case, image_key)
        created.append(cr)

    log.info("generated %d variants for case %s", len(created), case.id)
    return created


def generate_new() -> int:
    with session() as s:
        pairs = _cases_without_creative(s)
        log.info("generate_new: %d cases need creative", len(pairs))
        created = 0
        for case, icp in pairs:
            try:
                created += len(_generate_variants(s, case, icp))
            except Exception:
                log.exception("failed to generate creative for case %s", case.id)
        return created


# ---------- Pass 2: poll_videos ----------


def poll_videos() -> int:
    """For creatives with a submitted Arcads job but no downloaded video,
    check status and download when complete."""
    with session() as s:
        pending = s.scalars(
            select(Creative).where(
                Creative.arcads_job_id.is_not(None),
                Creative.video_s3_key.is_(None),
                Creative.status != CreativeStatus.rejected,
            )
        ).all()
        log.info("poll_videos: %d creatives waiting on Arcads", len(pending))
        done = 0
        for cr in pending:
            try:
                job = arcads_client.get_video(cr.arcads_job_id)
                if job.status == "completed" and job.video_url:
                    bytes_ = arcads_client.download_video(job.video_url)
                    key = f"creatives/{cr.id}.mp4"
                    storage.put_bytes(key, bytes_, "video/mp4")
                    cr.video_s3_key = key
                    cr.ai_disclosure_applied = True  # submitted with watermark_text
                    done += 1
                elif job.status == "failed":
                    cr.policy_check = {**cr.policy_check, "arcads_error": job.error}
                    # Leave status as pending_approval — reviewer can still approve
                    # the image-only ad, or reject it.
            except Exception:
                log.exception("arcads poll failed for creative %s", cr.id)
        return done


# ---------- Pass 1: poll_approvals ----------


def poll_approvals() -> tuple[int, int]:
    bot_id = slack_client.bot_user_id()
    with session() as s:
        pending = s.scalars(
            select(Creative).where(
                Creative.status == CreativeStatus.pending_approval,
                Creative.slack_message_ts.is_not(None),
            )
        ).all()
        log.info("poll_approvals: %d creatives awaiting human review", len(pending))
        approved = 0
        rejected = 0
        for cr in pending:
            try:
                rx = slack_client.get_human_reactions(cr.slack_message_ts, bot_user_id=bot_id)
            except Exception:
                log.exception("slack reaction poll failed for creative %s", cr.id)
                continue

            # Reject wins over approve if both present (safer default).
            if rx.reject_users:
                cr.status = CreativeStatus.rejected
                cr.approved_by = rx.reject_users[0]
                cr.approved_at = datetime.now(UTC)
                rejected += 1
            elif rx.approve_users:
                cr.status = CreativeStatus.approved
                cr.approved_by = rx.approve_users[0]
                cr.approved_at = datetime.now(UTC)
                approved += 1
        return approved, rejected


# ---------- Entrypoint ----------


@dataclass
class CreativeRunResult:
    approvals_approved: int
    approvals_rejected: int
    new_creatives: int


def run_once() -> CreativeRunResult:
    # poll_videos() is intentionally not called in MVP — video generation is
    # deferred (see _make_creative_row). The function is kept for re-enable.
    approved, rejected = poll_approvals()
    new_count = generate_new()
    result = CreativeRunResult(
        approvals_approved=approved,
        approvals_rejected=rejected,
        new_creatives=new_count,
    )
    log.info("creative run: %s", result)
    return result


def handler(_event: dict, _context: object) -> dict:
    r = run_once()
    return {
        "approved": r.approvals_approved,
        "rejected": r.approvals_rejected,
        "new_creatives": r.new_creatives,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(handler({}, None), indent=2))
