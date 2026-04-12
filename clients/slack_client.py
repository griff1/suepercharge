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

import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

APPROVE_EMOJI = "+1"
REJECT_EMOJI = "-1"

log = logging.getLogger(__name__)

# Stub mode:
#   LOCAL_STUB_SLACK=1          → write a JSON file per approval, auto-approve on poll
#   LOCAL_STUB_SLACK=reject     → auto-reject on poll instead
#   LOCAL_STUB_SLACK=manual     → write approval files, read ./.local-approvals/<ts>.reaction
#                                 ("approve" or "reject"); leaves creatives pending until
#                                 the user writes the file
# Unset AND no SLACK_BOT_TOKEN → same as LOCAL_STUB_SLACK=1 (auto-approve).

def _approval_dir() -> Path:
    return Path(os.environ.get("LOCAL_APPROVAL_DIR", ".local-approvals"))


def _stub_mode() -> str | None:
    val = os.environ.get("LOCAL_STUB_SLACK")
    if val in {"1", "true", "yes"}:
        return "approve"
    if val in {"reject", "approve", "manual"}:
        return val
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return "approve"
    return None


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
    """Post the creative to Slack with 👍/👎 reactions seeded. Returns the message ts.

    In stub mode (no Slack token, or LOCAL_STUB_SLACK set): writes a JSON file
    to `.local-approvals/<ts>.json` describing the creative, and returns a
    deterministic ts. The paired `get_human_reactions` either auto-resolves
    or reads a `.reaction` sibling file."""
    mode = _stub_mode()
    if mode is not None:
        ts = f"local-{uuid.uuid4().hex[:12]}"
        adir = _approval_dir()
        adir.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": ts,
            "mode": mode,
            "case_title": case_title,
            "case_url": case_url,
            "headline": headline,
            "primary_text": primary_text,
            "cta": cta,
            "image_url": image_url,
            "video_url": video_url,
        }
        (adir / f"{ts}.json").write_text(json.dumps(payload, indent=2))
        log.info("[stub:slack] wrote approval request %s (mode=%s) to %s",
                 ts, mode, adir)
        return ts

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
    # Stub: read a sidecar .reaction file, or resolve based on mode.
    if message_ts.startswith("local-") or _stub_mode() is not None:
        return _stub_reactions(message_ts)

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


def _stub_reactions(message_ts: str) -> ReactionResult:
    """Resolve a local approval request based on mode.

    - `approve` / unset / no token → auto-approve on first poll.
    - `reject`                     → auto-reject.
    - `manual` → look for `<ts>.reaction` sidecar containing "approve" or
      "reject"; otherwise return empty (leave pending)."""
    mode = _stub_mode() or "approve"
    if mode == "reject":
        return ReactionResult(approve_users=[], reject_users=["local-stub"])
    if mode == "manual":
        path = _approval_dir() / f"{message_ts}.reaction"
        if not path.exists():
            return ReactionResult(approve_users=[], reject_users=[])
        verdict = path.read_text().strip().lower()
        if verdict == "approve":
            return ReactionResult(approve_users=["local-manual"], reject_users=[])
        if verdict == "reject":
            return ReactionResult(approve_users=[], reject_users=["local-manual"])
        return ReactionResult(approve_users=[], reject_users=[])
    # approve (default)
    return ReactionResult(approve_users=["local-stub"], reject_users=[])


def bot_user_id() -> str | None:
    """Cached-per-invocation lookup of our own bot user id so we ignore our
    seeded reactions. Returns None on failure (agents should still work)."""
    if _stub_mode() is not None:
        return None
    try:
        with _client() as c:
            r = c.post("/auth.test")
            r.raise_for_status()
            body = r.json()
            _check(body)
            return body.get("user_id")
    except Exception:
        return None
