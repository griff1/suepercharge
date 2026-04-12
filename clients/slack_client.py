"""Slack client used for the creative approval gate.

Approval flow (by design, no Events API webhook):
  1. creative agent posts a message showing the generated copy + image + video
     link to the configured channel, seeds it with 👍 and 👎 reactions.
  2. Humans react 👍 to approve or 👎 to reject.
  3. On the next creative-agent cron tick, we call reactions.get on each
     pending_approval Creative's slack_message_ts; if a non-bot user reacted,
     flip the row's status.

This avoids standing up a Slack Events webhook (one fewer Function URL, one
fewer security boundary to harden) at the cost of latency equal to the cron
interval.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

APPROVE_EMOJI = "+1"
REJECT_EMOJI = "-1"


def _token() -> str:
    return os.environ["SLACK_BOT_TOKEN"]


def _channel() -> str:
    return os.environ["SLACK_CHANNEL_ID"]


def _client() -> httpx.Client:
    return httpx.Client(
        base_url="https://slack.com/api",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=20.0,
    )


def _check(resp_json: dict[str, Any]) -> None:
    if not resp_json.get("ok"):
        raise RuntimeError(f"Slack error: {resp_json.get('error')} ({resp_json})")


def post_creative_for_approval(
    *, headline: str, primary_text: str, cta: str, image_url: str | None, video_url: str | None,
    case_title: str, case_url: str,
) -> str:
    """Post the creative to Slack with 👍/👎 reactions seeded. Returns the message ts."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Creative awaiting approval"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Case:*\n<{case_url}|{case_title}>"},
                {"type": "mrkdwn", "text": f"*CTA:*\n{cta}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Headline*\n{headline}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Primary text*\n{primary_text}"}},
    ]
    if image_url:
        blocks.append({"type": "image", "image_url": image_url, "alt_text": "ad image"})
    if video_url:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Video:* {video_url}"}}
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "React :+1: to approve · :-1: to reject. This will deploy to Meta once approved.",
                }
            ],
        }
    )

    with _client() as c:
        r = c.post(
            "/chat.postMessage",
            json={"channel": _channel(), "blocks": blocks, "text": "Creative awaiting approval"},
        )
        r.raise_for_status()
        body = r.json()
        _check(body)
        ts = body["ts"]

        # Seed reactions so reviewers can just click. Ignore errors (e.g. already_reacted).
        for emoji in (APPROVE_EMOJI, REJECT_EMOJI):
            rr = c.post(
                "/reactions.add",
                json={"channel": _channel(), "name": emoji, "timestamp": ts},
            )
            # Non-fatal: we want the ts back even if seeding fails.
            if rr.status_code != 200 or not rr.json().get("ok"):
                # Common benign case: already_reacted
                pass

    return ts


@dataclass
class ReactionResult:
    approve_users: list[str]   # user ids excluding bots
    reject_users: list[str]


def get_human_reactions(message_ts: str, bot_user_id: str | None = None) -> ReactionResult:
    """Return the non-bot users who have reacted 👍/👎 to `message_ts`."""
    with _client() as c:
        r = c.get(
            "/reactions.get",
            params={"channel": _channel(), "timestamp": message_ts, "full": "true"},
        )
        r.raise_for_status()
        body = r.json()
        _check(body)

    message = body.get("message") or {}
    reactions = message.get("reactions", [])
    approve: list[str] = []
    reject: list[str] = []
    for rx in reactions:
        users = [u for u in rx.get("users", []) if u != bot_user_id]
        if rx.get("name") == APPROVE_EMOJI:
            approve.extend(users)
        elif rx.get("name") == REJECT_EMOJI:
            reject.extend(users)
    return ReactionResult(approve_users=approve, reject_users=reject)


def bot_user_id() -> str | None:
    """Cached-per-invocation lookup of our own bot user id so we ignore our
    seeded reactions. Returns None on failure (agents should still work)."""
    try:
        with _client() as c:
            r = c.post("/auth.test")
            r.raise_for_status()
            body = r.json()
            _check(body)
            return body.get("user_id")
    except Exception:
        return None
