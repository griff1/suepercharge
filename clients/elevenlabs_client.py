"""ElevenLabs Conversational AI client for outbound attorney calls.

Uses ElevenLabs' Conversational AI API with their native outbound calling.
The agent is configured server-side in the ElevenLabs dashboard; we only
dispatch calls and poll for transcripts here.

Stub activates when ELEVENLABS_API_KEY is missing or LOCAL_STUB_ELEVENLABS=1.
Stub writes a fake transcript to .local-calls/<conversation_id>.json.

ElevenLabs outbound call flow:
  1. POST /v1/convai/conversations/outbound  → conversation_id
  2. Poll GET /v1/convai/conversations/{id}  → status + transcript when done
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
_PHONE_NUMBER_ID = os.environ.get("ELEVENLABS_PHONE_NUMBER_ID", "")
_STUB = not _API_KEY or os.environ.get("LOCAL_STUB_ELEVENLABS", "0") == "1"
_BASE = "https://api.elevenlabs.io"
_LOCAL_DIR = Path(".local-calls")

# Polling config
_POLL_INTERVAL_S = float(os.environ.get("ELEVENLABS_POLL_INTERVAL_S", "30"))
_MAX_POLL_ATTEMPTS = int(os.environ.get("ELEVENLABS_MAX_POLL_ATTEMPTS", "20"))


@dataclass
class CallResult:
    conversation_id: str
    status: str          # "in_progress" | "done" | "failed"
    transcript: str | None
    duration_seconds: float | None


def place_call(*, phone_number: str, dynamic_variables: dict[str, str]) -> str:
    """Dispatch an outbound call. Returns conversation_id immediately.

    dynamic_variables are injected into the agent's prompt at call time, e.g.:
      {"attorney_name": "Jane Smith", "case_title": "Smith v. Acme Corp", ...}

    The caller must later poll get_call_result() to retrieve the transcript.
    """
    if _STUB:
        return _stub_place_call(phone_number, dynamic_variables)

    with httpx.Client(timeout=30.0) as client:
        body: dict = {
            "agent_id": _AGENT_ID,
            "agent_phone_number_id": _PHONE_NUMBER_ID,
            "to_number": phone_number,
        }
        if dynamic_variables:
            body["conversation_initiation_client_data"] = {
                "dynamic_variables": dynamic_variables,
            }
        resp = client.post(
            f"{_BASE}/v1/convai/twilio/outbound-call",
            headers={"xi-api-key": _API_KEY, "Content-Type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
    conversation_id = data["conversation_id"]
    log.info("ElevenLabs call placed: conversation_id=%s phone=%s", conversation_id, phone_number)
    return conversation_id


def get_call_result(conversation_id: str) -> CallResult:
    """Fetch the current status and transcript for a conversation.

    Call this on each agent tick; the transcript is only populated once
    status == "done".
    """
    if _STUB:
        return _stub_get_result(conversation_id)

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"{_BASE}/v1/convai/conversations/{conversation_id}",
            headers={"xi-api-key": _API_KEY},
        )
        resp.raise_for_status()
        data = resp.json()

    status = data.get("status", "unknown")
    transcript_turns = data.get("transcript", [])
    transcript_text: str | None = None
    if transcript_turns:
        lines = []
        for turn in transcript_turns:
            role = turn.get("role", "unknown")
            msg = turn.get("message", "")
            lines.append(f"{role.upper()}: {msg}")
        transcript_text = "\n".join(lines)

    return CallResult(
        conversation_id=conversation_id,
        status=status,
        transcript=transcript_text,
        duration_seconds=data.get("metadata", {}).get("call_duration_secs"),
    )


# ---------- Stub helpers ----------


def _stub_place_call(phone_number: str, dynamic_variables: dict[str, str]) -> str:
    _LOCAL_DIR.mkdir(exist_ok=True)
    ts = int(time.time() * 1000)
    conversation_id = f"stub-conv-{ts}"
    path = _LOCAL_DIR / f"{conversation_id}.json"
    path.write_text(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "phone_number": phone_number,
                "dynamic_variables": dynamic_variables,
                "status": "done",
                "transcript": [
                    {
                        "role": "agent",
                        "message": (
                            "This call is from an AI assistant. You are speaking with an "
                            "automated system, not a human. This call may be recorded. "
                            f"Hello, I'm calling for {dynamic_variables.get('attorney_name', 'the attorney')} "
                            f"regarding the {dynamic_variables.get('case_title', 'class action')} case."
                        ),
                    },
                    {
                        "role": "user",
                        "message": "Yes, this is the attorney. How can I help you?",
                    },
                    {
                        "role": "agent",
                        "message": (
                            f"We have {dynamic_variables.get('lead_count', '1')} qualified leads "
                            "for your case. They have expressed interest and consented to be contacted. "
                            f"Leads are available at {dynamic_variables.get('price_display', '$75')} each. "
                            "I'll send you a sample and our terms by email. Does that work for you?"
                        ),
                    },
                    {"role": "user", "message": "Sure, send it over. Sounds interesting."},
                ],
                "duration_seconds": 62.0,
            },
            indent=2,
        )
    )
    log.info("[ELEVENLABS STUB] placed call conversation_id=%s → %s", conversation_id, path)
    return conversation_id


def _stub_get_result(conversation_id: str) -> CallResult:
    path = _LOCAL_DIR / f"{conversation_id}.json"
    if not path.exists():
        return CallResult(
            conversation_id=conversation_id,
            status="in_progress",
            transcript=None,
            duration_seconds=None,
        )
    data = json.loads(path.read_text())
    turns = data.get("transcript", [])
    lines = [f"{t['role'].upper()}: {t['message']}" for t in turns]
    return CallResult(
        conversation_id=conversation_id,
        status=data.get("status", "done"),
        transcript="\n".join(lines) if lines else None,
        duration_seconds=data.get("duration_seconds"),
    )
