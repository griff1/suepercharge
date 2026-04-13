"""Deal agent — Stripe checkout creation, webhook handling, and PII release.

Two entrypoints:
  - handler(event, ctx)         — EventBridge cron (every 15 min): creates
    Stripe checkout sessions for follow_up_sent attempts that haven't got one,
    and sends the payment link email.
  - stripe_webhook_handler(event, ctx) — Lambda Function URL: handles Stripe
    checkout.session.completed to mark deals paid and release lead PII.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import compliance
from clients import ses_client, stripe_client
from db import session
from models import (
    Attorney,
    Case,
    Deal,
    DealStatus,
    Lead,
    OutreachAttempt,
    OutreachStage,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_SUEPERCHARGE_URL = os.environ.get("SUEPERCHARGE_URL", "https://suepercharge.ai")


# ---------- Checkout creation pass ----------


def _attempts_needing_checkout(s: Session) -> list[OutreachAttempt]:
    """follow_up_sent attempts that have no Deal row yet (non-free only)."""
    return list(
        s.scalars(
            select(OutreachAttempt).where(
                OutreachAttempt.stage == OutreachStage.follow_up_sent,
                OutreachAttempt.is_free.is_(False),
                ~OutreachAttempt.id.in_(
                    select(Deal.outreach_attempt_id)
                ),
            ).limit(20)
        ).all()
    )


def _create_deal_for_attempt(s: Session, attempt: OutreachAttempt) -> Deal | None:
    """Pick the best available lead for this attorney's case and create a Deal."""
    attorney = s.get(Attorney, attempt.attorney_id)
    case = s.get(Case, attorney.case_id) if attorney else None
    if not attorney or not case or not attorney.contact_email:
        return None

    # Pick a lead for this case that isn't already sold.
    lead = s.scalars(
        select(Lead)
        .join(Lead.campaign)
        .where(
            Lead.campaign.has(case_id=case.id),
            ~Lead.id.in_(select(Deal.lead_id).where(Deal.status == DealStatus.paid)),
        )
        .limit(1)
    ).first()

    if lead is None:
        log.warning("no available lead for case %s (attorney %s)", case.id, attorney.id)
        return None

    price_cents = attempt.price_cents or 7500
    description = f"Qualified lead — {case.title[:80]}"

    try:
        checkout = stripe_client.create_checkout_session(
            price_cents=price_cents,
            attorney_email=attorney.contact_email,
            metadata={
                "attorney_id": str(attorney.id),
                "lead_id": str(lead.id),
                "case_id": str(case.id),
                "outreach_attempt_id": str(attempt.id),
            },
            description=description,
        )
    except Exception:
        log.exception("Stripe session creation failed for attempt %s", attempt.id)
        return None

    deal = Deal(
        outreach_attempt_id=attempt.id,
        attorney_id=attorney.id,
        lead_id=lead.id,
        price_cents=price_cents,
        stripe_checkout_session_id=checkout.session_id,
        stripe_payment_intent_id=checkout.payment_intent_id,
        status=DealStatus.pending,
    )
    s.add(deal)
    s.flush()

    # Send payment link email.
    name = attorney.contact_name or "Counsel"
    body = (
        f"Dear {name},\n\n"
        f"Thank you for your interest in leads for the {case.title} case.\n\n"
        f"Your secure payment link is below. Once payment is confirmed, "
        f"we'll send the full lead details immediately.\n\n"
        f"Payment link: {checkout.url}\n\n"
        f"Amount: ${price_cents / 100:.2f}\n\n"
        f"This link expires in 24 hours. Reply to this email if you have questions.\n\n"
        f"{compliance.B2B_SERVICE_DISCLOSURE}\n"
    )
    try:
        ses_client.send_email(
            to=attorney.contact_email,
            subject=f"Payment link — lead for {case.title[:50]}",
            body_html=f"<pre>{body}</pre>",
            body_text=body,
        )
    except Exception:
        log.exception("payment link email failed for attorney %s", attorney.id)

    attempt.stage = OutreachStage.deal_created
    log.info(
        "deal %s created for attorney %s checkout=%s",
        deal.id, attorney.id, checkout.session_id,
    )
    return deal


def _handle_free_lead_release(s: Session) -> int:
    """For is_free=True follow_up_sent attempts, release the lead immediately."""
    free_attempts = list(
        s.scalars(
            select(OutreachAttempt).where(
                OutreachAttempt.stage == OutreachStage.follow_up_sent,
                OutreachAttempt.is_free.is_(True),
                ~OutreachAttempt.id.in_(select(Deal.outreach_attempt_id)),
            ).limit(10)
        ).all()
    )
    released = 0
    for attempt in free_attempts:
        attorney = s.get(Attorney, attempt.attorney_id)
        case = s.get(Case, attorney.case_id) if attorney else None
        if not attorney or not case or not attorney.contact_email:
            continue

        lead = s.scalars(
            select(Lead)
            .join(Lead.campaign)
            .where(Lead.campaign.has(case_id=case.id))
            .limit(1)
        ).first()
        if lead is None:
            continue

        deal = Deal(
            outreach_attempt_id=attempt.id,
            attorney_id=attorney.id,
            lead_id=lead.id,
            price_cents=0,
            status=DealStatus.paid,
            paid_at=datetime.now(UTC),
        )
        s.add(deal)
        s.flush()

        _release_pii(s, deal=deal, lead=lead, attorney=attorney, case=case)
        attempt.stage = OutreachStage.converted
        released += 1

    return released


def _release_pii(
    s: Session,
    *,
    deal: Deal,
    lead: Lead,
    attorney: Attorney,
    case: Case,
) -> None:
    """Email decrypted lead PII to the attorney and record the release."""
    if not attorney.contact_email:
        return

    # Decrypt PII (MVP: stored as plain UTF-8 bytes; see models.py comment).
    def _dec(b: bytes | None) -> str:
        return b.decode("utf-8") if b else "(not provided)"

    name = _dec(lead.name_enc)
    email = _dec(lead.email_enc)
    phone = _dec(lead.phone_enc)

    intake_text = ""
    if lead.intake_answers:
        lines = [f"  {k}: {v}" for k, v in lead.intake_answers.items()]
        intake_text = "\nIntake answers:\n" + "\n".join(lines)

    addressee = attorney.contact_name or "Counsel"
    body = (
        f"Dear {addressee},\n\n"
        f"Payment confirmed. Here are the lead details for {case.title}:\n\n"
        f"Name:  {name}\n"
        f"Email: {email}\n"
        f"Phone: {phone}\n"
        f"Consent version: {lead.consent_text_version}\n"
        f"Consented at: {lead.consent_ts.isoformat()}\n"
        f"{intake_text}\n\n"
        f"This individual consented under TCPA to be contacted by filing attorneys. "
        f"Please retain the consent version for your records.\n\n"
        f"{compliance.B2B_SERVICE_DISCLOSURE}\n"
    )
    try:
        ses_client.send_email(
            to=attorney.contact_email,
            subject=f"Lead delivered — {case.title[:55]}",
            body_html=f"<pre>{body}</pre>",
            body_text=body,
        )
    except Exception:
        log.exception("PII release email failed for deal %s", deal.id)
        return

    deal.pii_released_at = datetime.now(UTC)
    log.info("PII released for deal %s to attorney %s", deal.id, attorney.id)


def run_once() -> dict:
    result = {"deals_created": 0, "free_released": 0, "errored": 0}

    with session() as s:
        # Free lead releases first.
        result["free_released"] = _handle_free_lead_release(s)

        # Create checkout sessions for paid attempts.
        attempts = _attempts_needing_checkout(s)
        for attempt in attempts:
            try:
                deal = _create_deal_for_attempt(s, attempt)
                if deal:
                    result["deals_created"] += 1
            except Exception:
                log.exception("deal creation failed for attempt %s", attempt.id)
                result["errored"] += 1

    return result


def handler(_event: dict, _context: object) -> dict:
    return run_once()


# ---------- Stripe webhook handler (Lambda Function URL) ----------


def _http_response(status: int, body: str | dict) -> dict:
    if isinstance(body, dict):
        body = json.dumps(body)
        ct = "application/json"
    else:
        ct = "text/plain"
    return {"statusCode": status, "headers": {"content-type": ct}, "body": body}


def stripe_webhook_handler(event: dict, _context: object) -> dict:
    """Lambda Function URL handler for Stripe checkout webhooks."""
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "POST")
    if method != "POST":
        return _http_response(405, "method not allowed")

    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body_raw = base64.b64decode(body_raw)
    else:
        body_raw = body_raw.encode("utf-8") if isinstance(body_raw, str) else body_raw

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sig = headers.get("stripe-signature", "")

    try:
        stripe_event = stripe_client.construct_webhook_event(body_raw, sig)
    except Exception:
        log.exception("Stripe webhook signature verification failed")
        return _http_response(400, "invalid signature")

    if stripe_event.get("type") == "checkout.session.completed":
        cs = stripe_event.get("data", {}).get("object", {})
        session_id = cs.get("id")
        if session_id:
            _fulfill_checkout(session_id)

    return _http_response(200, {"received": True})


def _fulfill_checkout(session_id: str) -> None:
    try:
        checkout = stripe_client.get_checkout_session(session_id)
    except Exception:
        log.exception("failed to retrieve Stripe session %s", session_id)
        return

    if checkout.status != "complete":
        return

    with session() as s:
        deal = s.scalars(
            select(Deal).where(Deal.stripe_checkout_session_id == session_id)
        ).first()
        if deal is None:
            log.warning("no Deal row for Stripe session %s", session_id)
            return
        if deal.status == DealStatus.paid:
            return  # already processed (idempotent)

        deal.status = DealStatus.paid
        deal.paid_at = datetime.now(UTC)
        if checkout.payment_intent_id:
            deal.stripe_payment_intent_id = checkout.payment_intent_id

        # Update variant conversion counters.
        attempt = s.get(OutreachAttempt, deal.outreach_attempt_id)
        if attempt:
            attempt.stage = OutreachStage.converted
            for variant_key in [attempt.email_variant_key, attempt.call_variant_key]:
                if variant_key:
                    from models import PromptVariant
                    variant = s.scalars(
                        select(PromptVariant).where(
                            PromptVariant.variant_id == variant_key,
                            PromptVariant.is_active.is_(True),
                        )
                    ).first()
                    if variant:
                        variant.conversions += 1

        # Release PII.
        lead = s.get(Lead, deal.lead_id)
        attorney = s.get(Attorney, deal.attorney_id)
        case = s.get(Case, attorney.case_id) if attorney else None
        if lead and attorney and case:
            _release_pii(s, deal=deal, lead=lead, attorney=attorney, case=case)

        log.info("deal %s fulfilled (session=%s)", deal.id, session_id)


if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
