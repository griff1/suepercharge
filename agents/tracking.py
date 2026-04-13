"""Tracking webhook — Lambda Function URL that handles email open pixels,
"I'm interested" clicks, and unsubscribe links.

Every outreach email contains:
  1. A 1px tracking image:  <img src="{BASE}/t/{attempt_id}/open">
  2. An "I'm Interested" button:  <a href="{BASE}/t/{attempt_id}/interested">
  3. An unsubscribe link:  <a href="{BASE}/t/{attempt_id}/unsubscribe">

This handler records an EmailEvent for each hit and returns the appropriate
response (transparent gif for opens, redirect for clicks).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from db import session
from models import (
    Attorney,
    EmailEvent,
    EmailEventType,
    OutreachAttempt,
    OutreachStage,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# 1x1 transparent GIF (43 bytes).
_PIXEL_GIF = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

_THANK_YOU_URL = os.environ.get(
    "TRACKING_THANK_YOU_URL", "https://suepercharge.ai/thank-you"
)
_UNSUBSCRIBE_URL = os.environ.get(
    "TRACKING_UNSUBSCRIBE_URL", "https://suepercharge.ai/unsubscribed"
)


def _http_response(
    status: int,
    body: str | bytes,
    content_type: str = "text/plain",
    *,
    is_base64: bool = False,
    headers: dict | None = None,
) -> dict:
    h = {"content-type": content_type, "cache-control": "no-store"}
    if headers:
        h.update(headers)
    resp: dict = {"statusCode": status, "headers": h}
    if is_base64:
        resp["body"] = base64.b64encode(body).decode() if isinstance(body, bytes) else body
        resp["isBase64Encoded"] = True
    else:
        resp["body"] = body if isinstance(body, str) else body.decode()
    return resp


def _record_event(
    *,
    attempt_id_str: str,
    event_type: EmailEventType,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> bool:
    """Write an EmailEvent row. Returns True if recorded, False if attempt not found."""
    try:
        attempt_uuid = uuid.UUID(attempt_id_str)
    except ValueError:
        log.warning("invalid attempt_id in tracking URL: %s", attempt_id_str)
        return False

    with session() as s:
        attempt = s.get(OutreachAttempt, attempt_uuid)
        if attempt is None:
            log.warning("tracking hit for unknown attempt %s", attempt_id_str)
            return False

        event = EmailEvent(
            outreach_attempt_id=attempt.id,
            event_type=event_type,
            ses_message_id=attempt.ses_message_id,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        s.add(event)

        # Advance the stage if this event is a step forward.
        if event_type == EmailEventType.open and attempt.stage == OutreachStage.email_sent:
            attempt.stage = OutreachStage.email_opened

        elif event_type == EmailEventType.interested and attempt.stage in (
            OutreachStage.email_sent, OutreachStage.email_opened,
        ):
            attempt.stage = OutreachStage.interested

        elif event_type == EmailEventType.unsubscribe:
            attempt.stage = OutreachStage.unsubscribed
            attorney = s.get(Attorney, attempt.attorney_id)
            if attorney:
                attorney.outreach_blocked = True

        s.flush()
        log.info("recorded %s event for attempt %s", event_type, attempt_id_str)
    return True


def handler(event: dict, _context: object) -> dict:
    """Lambda Function URL handler. Routes: /t/{attempt_id}/{action}"""
    path = (event.get("rawPath") or event.get("path") or "").rstrip("/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    user_agent = headers.get("user-agent")
    # Extract IP from X-Forwarded-For (Lambda Function URL sets this).
    ip = (headers.get("x-forwarded-for") or "").split(",")[0].strip() or None

    # Parse /t/{attempt_id}/{action}
    parts = path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "t":
        return _http_response(404, "not found")

    attempt_id = parts[1]
    action = parts[2]

    if action == "open":
        _record_event(
            attempt_id_str=attempt_id,
            event_type=EmailEventType.open,
            user_agent=user_agent,
            ip_address=ip,
        )
        return _http_response(200, _PIXEL_GIF, "image/gif", is_base64=True)

    elif action == "interested":
        _record_event(
            attempt_id_str=attempt_id,
            event_type=EmailEventType.interested,
            user_agent=user_agent,
            ip_address=ip,
        )
        return _http_response(
            302, "", headers={"location": _THANK_YOU_URL}
        )

    elif action == "unsubscribe":
        _record_event(
            attempt_id_str=attempt_id,
            event_type=EmailEventType.unsubscribe,
            user_agent=user_agent,
            ip_address=ip,
        )
        return _http_response(
            302, "", headers={"location": _UNSUBSCRIBE_URL}
        )

    return _http_response(404, "unknown action")
