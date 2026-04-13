"""Outreach agent — human-gated email → track → Calendly → call → follow-up.

Cadence: EventBridge cron, every 15 minutes.

**Human-in-the-loop**: every outbound email or call is staged to
`pending_approval` first. Content is written to
`.local-approvals/outreach/<attempt_id>.json` for review.  Approve by
creating a `<attempt_id>.approved` sidecar file (or use
`scripts/approve_outreach.py`).  The next agent tick picks up approvals
and executes the action.

State machine per OutreachAttempt:
  (new attorney) → pending_approval (action=initial_email)
  approved → email_sent
  email_sent + open pixel → email_opened
  email_opened/sent + "interested" click → interested
  interested → pending_approval (action=calendly)
  approved → calendly_sent
  calendly_sent (after delay) → pending_approval (action=call)
  approved → call_placed
  call_placed + transcript → call_completed
  call_completed → pending_approval (action=follow_up)
  approved → follow_up_sent

Each tick:
  1. Execute approved pending actions.
  2. Advance existing attempts that are ready for their next step.
  3. Create new attempts for attorneys that haven't been contacted yet.
  4. Expire stale attempts.
"""
from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jinja2 import Environment
from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.orm import Session

import compliance
from clients import elevenlabs_client, ses_client
from db import session
from models import (
    Attorney,
    Case,
    EmailEvent,
    EmailEventType,
    Lead,
    OutreachAttempt,
    OutreachStage,
    PromptVariant,
)
from prompts import render

APPROVAL_DIR = Path(".local-approvals/outreach")

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

ATTEMPT_EXPIRY_DAYS = int(os.environ.get("OUTREACH_EXPIRY_DAYS", "7"))
CALENDLY_URL = os.environ.get("CALENDLY_URL", "https://calendly.com/suepercharge/lead-discussion")
CALL_DELAY_HOURS = int(os.environ.get("CALL_DELAY_HOURS", "2"))
HAIKU_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")


# ---------- A/B variant selection ----------


def pick_variant(s: Session, experiment_key: str) -> PromptVariant | None:
    """Weighted random selection from active variants. Records the impression."""
    variants = list(
        s.scalars(
            select(PromptVariant).where(
                PromptVariant.experiment_key == experiment_key,
                PromptVariant.is_active.is_(True),
            )
        ).all()
    )
    if not variants:
        return None
    weights = [v.weight for v in variants]
    chosen = random.choices(variants, weights=weights, k=1)[0]
    chosen.impressions += 1
    return chosen


# ---------- Email generation ----------


def _generate_email(
    *,
    attorney: Attorney,
    case: Case,
    lead_count: int,
    email_variant: PromptVariant | None,
) -> tuple[str, str, str]:
    """Returns (subject, html_body, plain_body)."""
    context = {
        "attorney_name": attorney.contact_name or "Counsel",
        "firm_name": attorney.firm_name,
        "case_title": case.title,
        "defendants": ", ".join(case.defendants or []),
        "lead_count": lead_count,
        "deadline": str(case.deadline) if case.deadline else "not specified",
    }

    if email_variant:
        env = Environment(autoescape=False)
        prompt_text = env.from_string(email_variant.content).render(**context)
    else:
        prompt_text = render("email_pitch", **context, is_free=True, price_display="no charge")

    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt_text}],
    )
    raw = msg.content[0].text.strip()

    subject = ""
    body = raw
    if "---SUBJECT---" in raw and "---BODY---" in raw:
        parts = raw.split("---BODY---", 1)
        body = parts[1].strip()
        subj_part = parts[0].split("---SUBJECT---", 1)
        subject = subj_part[1].strip() if len(subj_part) > 1 else ""

    if not subject:
        subject = f"Qualified leads for {case.title[:40]}"

    if not compliance.b2b_copy_is_clean(body):
        log.warning("email copy compliance violation for attorney %s — using fallback", attorney.id)
        body = _safe_fallback_email(attorney=attorney, case=case, lead_count=lead_count)
        subject = f"Qualified leads — {case.title[:40]}"

    html_body = f"<div style='font-family:sans-serif;line-height:1.6;max-width:600px'>{_text_to_html(body)}</div>"
    return subject, html_body, body


def _text_to_html(text: str) -> str:
    """Simple text → HTML conversion preserving paragraphs."""
    paragraphs = text.split("\n\n")
    return "".join(f"<p>{p.replace(chr(10), '<br/>')}</p>" for p in paragraphs if p.strip())


def _safe_fallback_email(*, attorney: Attorney, case: Case, lead_count: int) -> str:
    name = attorney.contact_name or "Counsel"
    return (
        f"Dear {name},\n\n"
        f"I'm reaching out from Suepercharge regarding the {case.title} case. "
        f"We ran digital advertising for this class action and captured {lead_count} "
        f"qualified leads who expressed interest and consented under TCPA to be "
        f"contacted by the filing attorneys.\n\n"
        f"Each lead completed our intake form and confirmed eligibility. "
        f"I'd be happy to share details — just click the link below.\n\n"
        f"{compliance.B2B_SERVICE_DISCLOSURE}\n"
    )


# ---------- Approval helpers ----------


def _stage_for_approval(
    s: Session,
    attempt: OutreachAttempt,
    action: str,
    content: dict,
) -> None:
    """Stage an outreach action for human approval. Writes a review file to disk."""
    attempt.stage = OutreachStage.pending_approval
    attempt.pending_action = action
    attempt.pending_content = content

    # Write review file so humans can inspect without DB access.
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    review_path = APPROVAL_DIR / f"{attempt.id}.json"
    review_path.write_text(json.dumps({
        "attempt_id": str(attempt.id),
        "action": action,
        "attorney_id": str(attempt.attorney_id),
        **content,
    }, indent=2))
    log.info(
        "staged %s for approval → %s (approve with: scripts/approve_outreach.py %s)",
        action, review_path, attempt.id,
    )


def _check_approved(attempt_id: str) -> bool:
    """Return True if a .approved sidecar file exists for this attempt."""
    return (APPROVAL_DIR / f"{attempt_id}.approved").exists()


def _execute_approved(s: Session, attempt: OutreachAttempt) -> bool:
    """Execute a previously approved pending action. Returns True on success."""
    action = attempt.pending_action
    content = attempt.pending_content or {}
    attorney = s.get(Attorney, attempt.attorney_id)
    case = s.get(Case, attorney.case_id) if attorney else None

    if not attorney or not case:
        return False

    attempt.approved_at = datetime.now(UTC)
    attempt.approved_by = "cli"

    if action == "initial_email":
        return _exec_send_email(s, attempt, attorney, content)
    elif action == "calendly":
        return _exec_send_calendly(s, attempt, attorney, case, content)
    elif action == "call":
        return _exec_place_call(s, attempt, attorney, case)
    elif action == "follow_up":
        return _exec_send_follow_up(s, attempt, attorney, case, content)
    else:
        log.error("unknown pending action %r for attempt %s", action, attempt.id)
        return False


def _exec_send_email(
    s: Session, attempt: OutreachAttempt, attorney: Attorney, content: dict,
) -> bool:
    """Actually send the staged initial email after approval."""
    try:
        msg_id = ses_client.send_email(
            to=attorney.contact_email,
            subject=content["subject"],
            body_html=content["html_body"],
            body_text=content["text_body"],
            attempt_id=str(attempt.id),
        )
        attempt.ses_message_id = msg_id
        attempt.stage = OutreachStage.email_sent
    except Exception:
        log.exception("SES send failed for attorney %s", attorney.id)
        attempt.last_error = "initial email send failed"
        return False

    s.add(EmailEvent(
        outreach_attempt_id=attempt.id,
        event_type=EmailEventType.send,
        ses_message_id=msg_id,
    ))
    log.info("approved email sent to %s attempt=%s", attorney.contact_email, attempt.id)
    return True


def _exec_send_calendly(
    s: Session, attempt: OutreachAttempt, attorney: Attorney, case: Case, content: dict,
) -> bool:
    try:
        ses_client.send_email(
            to=attorney.contact_email,
            subject=content["subject"],
            body_html=content["html_body"],
            body_text=content["text_body"],
            attempt_id=str(attempt.id),
        )
    except Exception:
        log.exception("Calendly email failed for attorney %s", attorney.id)
        return False
    attempt.stage = OutreachStage.calendly_sent
    attempt.calendly_link_sent = True
    log.info("approved Calendly email sent to %s attempt=%s", attorney.contact_email, attempt.id)
    return True


def _exec_place_call(
    s: Session, attempt: OutreachAttempt, attorney: Attorney, case: Case,
) -> bool:
    if not attorney.contact_phone:
        attempt.stage = OutreachStage.follow_up_sent
        return True

    call_variant = pick_variant(s, "call_script")
    lead_count = s.scalar(
        select(sqlfunc.count(Lead.id)).join(Lead.campaign).where(Lead.campaign.has(case_id=case.id))
    ) or 1

    try:
        conversation_id = elevenlabs_client.place_call(
            phone_number=attorney.contact_phone,
            dynamic_variables={
                "attorney_name": attorney.contact_name or attorney.firm_name,
                "firm_name": attorney.firm_name,
                "case_title": case.title,
                "defendants": ", ".join(case.defendants or []),
                "lead_count": str(lead_count),
                "contact_email": attorney.contact_email or "",
            },
        )
    except Exception:
        log.exception("ElevenLabs call failed for attorney %s", attorney.id)
        attempt.last_error = "call placement failed"
        return False

    attempt.stage = OutreachStage.call_placed
    attempt.call_conversation_id = conversation_id
    attempt.call_variant_key = call_variant.variant_id if call_variant else None
    log.info("approved call placed for attorney %s conversation=%s", attorney.id, conversation_id)
    return True


def _exec_send_follow_up(
    s: Session, attempt: OutreachAttempt, attorney: Attorney, case: Case, content: dict,
) -> bool:
    try:
        ses_client.send_email(
            to=attorney.contact_email,
            subject=content["subject"],
            body_html=content["html_body"],
            body_text=content["text_body"],
            attempt_id=str(attempt.id),
        )
    except Exception:
        log.exception("follow-up email failed for attorney %s", attorney.id)
        return False
    attempt.stage = OutreachStage.follow_up_sent
    log.info("approved follow-up sent to %s attempt=%s", attorney.contact_email, attempt.id)
    return True


# ---------- Step handlers (now stage for approval instead of sending) ----------


def _step_send_initial_email(s: Session, attorney: Attorney) -> OutreachAttempt | None:
    """Generate the pitch email and stage it for human approval."""
    case = s.get(Case, attorney.case_id)
    if not case or not attorney.contact_email or attorney.outreach_blocked:
        return None

    email_variant = pick_variant(s, "email_subject_body")

    lead_count = s.scalar(
        select(sqlfunc.count(Lead.id))
        .join(Lead.campaign)
        .where(Lead.campaign.has(case_id=case.id))
    ) or 0
    lead_count = max(lead_count, 1)

    subject, html_body, text_body = _generate_email(
        attorney=attorney,
        case=case,
        lead_count=lead_count,
        email_variant=email_variant,
    )

    attempt = OutreachAttempt(
        attorney_id=attorney.id,
        stage=OutreachStage.pending_approval,
        email_variant_key=email_variant.variant_id if email_variant else None,
        email_subject=subject,
        expires_at=datetime.now(UTC) + timedelta(days=ATTEMPT_EXPIRY_DAYS),
    )
    s.add(attempt)
    s.flush()

    _stage_for_approval(s, attempt, "initial_email", {
        "to": attorney.contact_email,
        "firm": attorney.firm_name,
        "attorney": attorney.contact_name,
        "case": case.title,
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
    })
    return attempt


def _step_send_calendly(s: Session, attempt: OutreachAttempt) -> None:
    """Stage Calendly email for human approval."""
    attorney = s.get(Attorney, attempt.attorney_id)
    case = s.get(Case, attorney.case_id) if attorney else None
    if not attorney or not case or not attorney.contact_email:
        return

    name = attorney.contact_name or "Counsel"
    subject = f"Re: {attempt.email_subject or case.title[:40]} — let's schedule a call"
    body = (
        f"Dear {name},\n\n"
        f"Thank you for your interest in the leads for {case.title}.\n\n"
        f"I'd love to walk you through what we have. Please book a time "
        f"that works for you:\n\n"
        f"{CALENDLY_URL}\n\n"
        f"If you'd prefer, our AI assistant can call you directly — "
        f"just reply to this email with a good time.\n\n"
        f"Looking forward to connecting.\n\n"
        f"{compliance.B2B_SERVICE_DISCLOSURE}\n"
    )
    html_body = (
        f"<div style='font-family:sans-serif;line-height:1.6;max-width:600px'>"
        f"<p>Dear {name},</p>"
        f"<p>Thank you for your interest in the leads for <strong>{case.title}</strong>.</p>"
        f"<p>I'd love to walk you through what we have. Please book a time:</p>"
        f"<p><a href='{CALENDLY_URL}' style='background:#2563eb;color:#fff;"
        f"padding:12px 24px;text-decoration:none;border-radius:6px;"
        f"font-weight:bold;display:inline-block;'>Schedule a Call</a></p>"
        f"<p>If you'd prefer, our AI assistant can call you directly — "
        f"just reply with a good time.</p>"
        f"<p style='font-size:12px;color:#666;'>{compliance.B2B_SERVICE_DISCLOSURE}</p>"
        f"</div>"
    )

    _stage_for_approval(s, attempt, "calendly", {
        "to": attorney.contact_email,
        "firm": attorney.firm_name,
        "attorney": attorney.contact_name,
        "case": case.title,
        "subject": subject,
        "html_body": html_body,
        "text_body": body,
    })


def _step_place_call(s: Session, attempt: OutreachAttempt) -> None:
    """Stage an ElevenLabs call for human approval."""
    attorney = s.get(Attorney, attempt.attorney_id)
    case = s.get(Case, attorney.case_id) if attorney else None
    if not attorney or not case:
        return

    if not attorney.contact_phone:
        log.info("attorney %s has no phone — skipping call, advancing to follow_up", attorney.id)
        attempt.stage = OutreachStage.call_completed
        return

    # Don't call too soon after Calendly email.
    if attempt.updated_at and (datetime.now(UTC) - attempt.updated_at).total_seconds() < CALL_DELAY_HOURS * 3600:
        return

    _stage_for_approval(s, attempt, "call", {
        "to_phone": attorney.contact_phone,
        "firm": attorney.firm_name,
        "attorney": attorney.contact_name,
        "case": case.title,
    })


def _step_poll_call(s: Session, attempt: OutreachAttempt) -> None:
    """Check if ElevenLabs call has completed and store the transcript."""
    if not attempt.call_conversation_id:
        attempt.stage = OutreachStage.call_completed
        return

    try:
        result = elevenlabs_client.get_call_result(attempt.call_conversation_id)
    except Exception:
        log.exception("ElevenLabs poll failed for conversation %s", attempt.call_conversation_id)
        return

    if result.status == "in_progress":
        return

    attempt.call_transcript = result.transcript

    if result.transcript:
        try:
            summary_prompt = render("summarize_call", transcript=result.transcript)
            import anthropic
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            attempt.call_summary = msg.content[0].text.strip()
        except Exception:
            log.exception("call summary failed for attempt %s", attempt.id)

    attempt.stage = OutreachStage.call_completed
    log.info("call completed for attempt %s", attempt.id)


def _step_send_follow_up(s: Session, attempt: OutreachAttempt) -> None:
    """Stage post-call follow-up email for human approval."""
    attorney = s.get(Attorney, attempt.attorney_id)
    case = s.get(Case, attorney.case_id) if attorney else None
    if not attorney or not case or not attorney.contact_email:
        attempt.stage = OutreachStage.dead
        return

    name = attorney.contact_name or "Counsel"

    summary_text = ""
    if attempt.call_summary:
        try:
            data = json.loads(attempt.call_summary)
            summary_text = data.get("summary", attempt.call_summary)
        except (json.JSONDecodeError, AttributeError):
            summary_text = attempt.call_summary

    subject = f"Follow-up: {case.title[:50]}"
    body = (
        f"Dear {name},\n\n"
        f"Thank you for speaking with our AI assistant regarding {case.title}.\n\n"
    )
    if summary_text:
        body += f"Call summary:\n{summary_text}\n\n"
    body += (
        f"Next steps: if you'd like to proceed with receiving qualified leads "
        f"for this case, please reply to this email or book a follow-up:\n"
        f"{CALENDLY_URL}\n\n"
        f"{compliance.B2B_SERVICE_DISCLOSURE}\n"
    )
    html_body = f"<div style='font-family:sans-serif;line-height:1.6;max-width:600px'>{_text_to_html(body)}</div>"

    _stage_for_approval(s, attempt, "follow_up", {
        "to": attorney.contact_email,
        "firm": attorney.firm_name,
        "attorney": attorney.contact_name,
        "case": case.title,
        "subject": subject,
        "html_body": html_body,
        "text_body": body,
    })


def _expire_stale(s: Session) -> int:
    """Mark attempts past expiry as dead."""
    stale = list(
        s.scalars(
            select(OutreachAttempt).where(
                OutreachAttempt.stage.in_([
                    OutreachStage.email_sent,
                    OutreachStage.email_opened,
                    OutreachStage.calendly_sent,
                    OutreachStage.follow_up_sent,
                ]),
                OutreachAttempt.expires_at < datetime.now(UTC),
            )
        ).all()
    )
    for a in stale:
        a.stage = OutreachStage.dead
    if stale:
        log.info("expired %d stale outreach attempts", len(stale))
    return len(stale)


# ---------- Orchestration ----------


@dataclass
class OutreachResult:
    approved: int = 0
    new_staged: int = 0
    calendly_staged: int = 0
    calls_staged: int = 0
    calls_polled: int = 0
    follow_ups_staged: int = 0
    expired: int = 0
    errored: int = 0


def run_once() -> OutreachResult:
    result = OutreachResult()

    with session() as s:
        result.expired = _expire_stale(s)

        # --- 1. Execute approved pending actions ---
        pending = list(
            s.scalars(
                select(OutreachAttempt).where(
                    OutreachAttempt.stage == OutreachStage.pending_approval,
                )
            ).all()
        )
        for attempt in pending:
            if _check_approved(str(attempt.id)):
                try:
                    if _execute_approved(s, attempt):
                        result.approved += 1
                        # Clean up sidecar files
                        (APPROVAL_DIR / f"{attempt.id}.json").unlink(missing_ok=True)
                        (APPROVAL_DIR / f"{attempt.id}.approved").unlink(missing_ok=True)
                except Exception:
                    log.exception("failed executing approved action for attempt %s", attempt.id)
                    result.errored += 1

        # --- 2. Advance existing attempts (stage for approval) ---
        active = list(
            s.scalars(
                select(OutreachAttempt).where(
                    OutreachAttempt.stage.in_([
                        OutreachStage.interested,
                        OutreachStage.calendly_sent,
                        OutreachStage.call_placed,
                        OutreachStage.call_completed,
                    ])
                ).limit(50)
            ).all()
        )

        for attempt in active:
            try:
                if attempt.stage == OutreachStage.interested:
                    _step_send_calendly(s, attempt)
                    result.calendly_staged += 1
                elif attempt.stage == OutreachStage.calendly_sent:
                    _step_place_call(s, attempt)
                    if attempt.stage == OutreachStage.pending_approval:
                        result.calls_staged += 1
                elif attempt.stage == OutreachStage.call_placed:
                    _step_poll_call(s, attempt)
                    result.calls_polled += 1
                elif attempt.stage == OutreachStage.call_completed:
                    _step_send_follow_up(s, attempt)
                    result.follow_ups_staged += 1
            except Exception:
                log.exception("outreach step failed for attempt %s", attempt.id)
                result.errored += 1

        # --- 3. Create new attempts for uncontacted attorneys (staged) ---
        attorneys = list(
            s.scalars(
                select(Attorney).where(
                    Attorney.outreach_blocked.is_(False),
                    Attorney.contact_email.isnot(None),
                    ~Attorney.id.in_(
                        select(OutreachAttempt.attorney_id)
                    ),
                ).limit(10)
            ).all()
        )

        for attorney in attorneys:
            try:
                if _step_send_initial_email(s, attorney):
                    result.new_staged += 1
            except Exception:
                log.exception("failed to stage outreach for attorney %s", attorney.id)
                result.errored += 1

    log.info("outreach run: %s", result)
    return result


def handler(_event: dict, _context: object) -> dict:
    r = run_once()
    return {
        "approved": r.approved,
        "new_staged": r.new_staged,
        "calendly_staged": r.calendly_staged,
        "calls_staged": r.calls_staged,
        "calls_polled": r.calls_polled,
        "follow_ups_staged": r.follow_ups_staged,
        "expired": r.expired,
        "errored": r.errored,
    }


if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
