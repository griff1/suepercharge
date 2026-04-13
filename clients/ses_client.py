"""Email client — Resend API with tracking injection and local stub.

Every outgoing email gets:
  1. A 1px tracking pixel for open detection
  2. An "I'm Interested" CTA button
  3. An unsubscribe link
  4. The B2B service compliance disclosure

Stub activates when RESEND_API_KEY is missing or LOCAL_STUB_EMAIL=1.
Stub writes emails to .local-emails/<timestamp>_<to>.json.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_API_KEY = os.environ.get("RESEND_API_KEY", "")
_FROM_EMAIL = os.environ.get("EMAIL_FROM", "Suepercharge <onboarding@resend.dev>")
_STUB = not _API_KEY or os.environ.get("LOCAL_STUB_EMAIL", "0") == "1"
_LOCAL_DIR = Path(".local-emails")

TRACKING_BASE_URL = os.environ.get("TRACKING_WEBHOOK_URL", "http://localhost:8787/t")


def _inject_tracking(body_html: str, body_text: str, attempt_id: str) -> tuple[str, str]:
    """Add open pixel, interested CTA, and unsubscribe link."""
    base = TRACKING_BASE_URL.rstrip("/")
    open_url = f"{base}/{attempt_id}/open"
    interested_url = f"{base}/{attempt_id}/interested"
    unsub_url = f"{base}/{attempt_id}/unsubscribe"

    pixel = f'<img src="{open_url}" width="1" height="1" alt="" style="display:none" />'
    cta_html = (
        f'<br/><br/>'
        f'<a href="{interested_url}" style="background:#2563eb;color:#fff;'
        f'padding:12px 24px;text-decoration:none;border-radius:6px;'
        f'font-weight:bold;display:inline-block;">Yes, I\'m Interested</a>'
    )
    unsub_html = (
        f'<br/><br/><hr style="border:none;border-top:1px solid #ddd;margin:20px 0"/>'
        f'<p style="font-size:11px;color:#999;">'
        f'<a href="{unsub_url}" style="color:#999;">Unsubscribe</a> '
        f'from Suepercharge lead notifications.</p>'
    )

    html_out = body_html + cta_html + unsub_html + pixel
    text_out = (
        body_text
        + f"\n\n---\nInterested? Visit: {interested_url}"
        + f"\nUnsubscribe: {unsub_url}"
    )
    return html_out, text_out


def send_email(
    *,
    to: str,
    subject: str,
    body_html: str,
    body_text: str,
    attempt_id: str | None = None,
) -> str:
    """Send a transactional email via Resend. Returns the message ID."""
    if attempt_id:
        body_html, body_text = _inject_tracking(body_html, body_text, attempt_id)

    if _STUB:
        return _stub_send(to, subject, body_html, body_text)

    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": _FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "html": body_html,
            "text": body_text,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    msg_id = data.get("id", "unknown")
    log.info("Resend sent email to %s (id=%s)", to, msg_id)
    return msg_id


def _stub_send(to: str, subject: str, body_html: str, body_text: str) -> str:
    _LOCAL_DIR.mkdir(exist_ok=True)
    ts = int(time.time() * 1000)
    safe_to = to.replace("@", "_at_").replace(".", "_")
    path = _LOCAL_DIR / f"{ts}_{safe_to}.json"
    payload = {
        "to": to,
        "from": _FROM_EMAIL,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
    }
    path.write_text(json.dumps(payload, indent=2))
    msg_id = f"stub-email-{ts}"
    log.info("[EMAIL STUB] wrote email → %s (id=%s)", path, msg_id)
    return msg_id
