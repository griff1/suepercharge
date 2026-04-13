"""Stripe client for lead purchase checkout sessions.

Stub activates when STRIPE_SECRET_KEY is missing or LOCAL_STUB_STRIPE=1.
Stub writes session objects to .local-stripe/<session_id>.json and
immediately marks them as paid (for end-to-end local testing).

Flow:
  1. deal.py calls create_checkout_session() → Stripe hosted checkout URL
  2. Attorney completes payment → Stripe sends checkout.session.completed webhook
  3. deal.py webhook_handler calls fulfill_checkout_session() with the session ID
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
_STUB = not _SECRET_KEY or os.environ.get("LOCAL_STUB_STRIPE", "0") == "1"
_LOCAL_DIR = Path(".local-stripe")

# Stripe webhook signing secret — used to verify incoming webhook payloads.
_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Public success/cancel redirect URLs (the attorney lands here post-payment).
_SUCCESS_URL = os.environ.get(
    "STRIPE_SUCCESS_URL", "https://suepercharge.ai/lead-purchased?session_id={CHECKOUT_SESSION_ID}"
)
_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL", "https://suepercharge.ai/lead-purchase-cancelled")


@dataclass
class CheckoutSession:
    session_id: str
    payment_intent_id: str | None
    url: str
    status: str   # "open" | "complete" | "expired"


def create_checkout_session(
    *,
    price_cents: int,
    attorney_email: str,
    metadata: dict[str, str],
    description: str,
) -> CheckoutSession:
    """Create a Stripe Checkout Session for a single lead purchase.

    metadata should include at minimum: deal_id, attorney_id, lead_id.
    Returns the session so the caller can extract the payment URL to email.
    """
    if _STUB:
        return _stub_create_session(price_cents, attorney_email, metadata, description)

    import stripe  # lazy import

    stripe.api_key = _SECRET_KEY
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": price_cents,
                    "product_data": {"name": description},
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        customer_email=attorney_email,
        metadata=metadata,
        success_url=_SUCCESS_URL,
        cancel_url=_CANCEL_URL,
    )
    log.info(
        "Stripe checkout session created: %s price=%d attorney=%s",
        session.id, price_cents, attorney_email,
    )
    return CheckoutSession(
        session_id=session.id,
        payment_intent_id=session.payment_intent,
        url=session.url,
        status=session.status,
    )


def get_checkout_session(session_id: str) -> CheckoutSession:
    """Retrieve a checkout session by ID. Used in webhook handling."""
    if _STUB:
        return _stub_get_session(session_id)

    import stripe

    stripe.api_key = _SECRET_KEY
    session = stripe.checkout.Session.retrieve(session_id)
    return CheckoutSession(
        session_id=session.id,
        payment_intent_id=session.payment_intent,
        url=session.url or "",
        status=session.status,
    )


def construct_webhook_event(payload: bytes, sig_header: str) -> dict:
    """Verify and parse a Stripe webhook payload. Raises on bad signature."""
    if _STUB:
        return json.loads(payload)

    import stripe

    stripe.api_key = _SECRET_KEY
    event = stripe.Webhook.construct_event(payload, sig_header, _WEBHOOK_SECRET)
    return event  # type: ignore[return-value]


# ---------- Stub helpers ----------


def _stub_create_session(
    price_cents: int,
    attorney_email: str,
    metadata: dict[str, str],
    description: str,
) -> CheckoutSession:
    _LOCAL_DIR.mkdir(exist_ok=True)
    ts = int(time.time() * 1000)
    session_id = f"stub-cs-{ts}"
    data = {
        "session_id": session_id,
        "payment_intent_id": f"stub-pi-{ts}",
        "url": f"https://checkout.stripe.com/stub/{session_id}",
        "status": "complete",   # stub auto-completes for local testing
        "price_cents": price_cents,
        "attorney_email": attorney_email,
        "metadata": metadata,
        "description": description,
    }
    path = _LOCAL_DIR / f"{session_id}.json"
    path.write_text(json.dumps(data, indent=2))
    log.info("[STRIPE STUB] created checkout session %s → %s", session_id, path)
    return CheckoutSession(
        session_id=session_id,
        payment_intent_id=data["payment_intent_id"],
        url=data["url"],
        status="complete",
    )


def _stub_get_session(session_id: str) -> CheckoutSession:
    path = _LOCAL_DIR / f"{session_id}.json"
    if not path.exists():
        return CheckoutSession(
            session_id=session_id,
            payment_intent_id=None,
            url="",
            status="open",
        )
    data = json.loads(path.read_text())
    return CheckoutSession(
        session_id=session_id,
        payment_intent_id=data.get("payment_intent_id"),
        url=data.get("url", ""),
        status=data.get("status", "complete"),
    )
