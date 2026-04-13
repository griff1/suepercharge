"""Quick ElevenLabs API connectivity + agent verification test.

Tests:
  1. API key is valid (GET /v1/user)
  2. Agent exists and is configured (GET /v1/convai/agents/{id})
  3. Expected dynamic variables are present on the agent
  4. (Optional) Place a test call if --call +1XXXXXXXXXX is passed

Usage:
  uv run scripts/test_elevenlabs.py               # connectivity only
  uv run scripts/test_elevenlabs.py --call +15551234567  # actually place a call
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
PHONE_NUMBER_ID = os.environ.get("ELEVENLABS_PHONE_NUMBER_ID", "")
BASE = "https://api.elevenlabs.io"

EXPECTED_VARS = {"attorney_name", "firm_name", "case_title", "lead_count", "contact_email"}


def _headers() -> dict[str, str]:
    return {"xi-api-key": API_KEY}


def test_api_key() -> bool:
    """Verify the API key is valid by fetching user info."""
    print("1) Testing API key...", end=" ")
    try:
        r = httpx.get(f"{BASE}/v1/user", headers=_headers(), timeout=15.0)
        if r.status_code == 401:
            print("FAIL — 401 Unauthorized. Check ELEVENLABS_API_KEY.")
            return False
        r.raise_for_status()
        data = r.json()
        tier = data.get("subscription", {}).get("tier", "unknown")
        chars_left = data.get("subscription", {}).get("character_count", "?")
        chars_limit = data.get("subscription", {}).get("character_limit", "?")
        print(f"OK — tier={tier}, characters={chars_left}/{chars_limit}")
        return True
    except httpx.HTTPError as e:
        print(f"FAIL — {e}")
        return False


def test_agent() -> dict | None:
    """Verify the agent exists and return its config."""
    print(f"2) Fetching agent {AGENT_ID}...", end=" ")
    if not AGENT_ID:
        print("SKIP — ELEVENLABS_AGENT_ID not set.")
        return None
    try:
        r = httpx.get(
            f"{BASE}/v1/convai/agents/{AGENT_ID}",
            headers=_headers(),
            timeout=15.0,
        )
        if r.status_code == 404:
            print("FAIL — agent not found. Check ELEVENLABS_AGENT_ID.")
            return None
        r.raise_for_status()
        data = r.json()
        name = data.get("name", "(unnamed)")
        print(f"OK — name={name!r}")
        return data
    except httpx.HTTPError as e:
        print(f"FAIL — {e}")
        return None


def check_dynamic_vars(agent_data: dict) -> None:
    """Check that expected dynamic variables are configured on the agent."""
    print("3) Checking dynamic variables...", end=" ")
    # ElevenLabs stores dynamic vars in the conversation config
    conv_config = agent_data.get("conversation_config", {})
    agent_config = conv_config.get("agent", {})
    prompt_config = agent_config.get("prompt", {})

    # Check system prompt for variable references
    system_prompt = prompt_config.get("prompt", "")

    found = set()
    missing = set()
    for var in EXPECTED_VARS:
        if f"{{{{{var}}}}}" in system_prompt or var in system_prompt:
            found.add(var)
        else:
            missing.add(var)

    if missing:
        print(f"WARNING — missing from prompt: {missing}")
    else:
        print(f"OK — all {len(found)} variables referenced in prompt")

    # Print first message
    first_msg = conv_config.get("agent", {}).get("first_message", "")
    if first_msg:
        print(f"   First message: {first_msg[:100]}...")


def test_phone_numbers() -> str | None:
    """List phone numbers connected to the account and return the first ID."""
    print("4) Listing phone numbers...", end=" ")
    try:
        r = httpx.get(
            f"{BASE}/v1/convai/phone-numbers",
            headers=_headers(),
            timeout=15.0,
        )
        r.raise_for_status()
        numbers = r.json()
        if isinstance(numbers, list) and numbers:
            for n in numbers:
                pid = n.get("phone_number_id") or n.get("id", "?")
                label = n.get("phone_number") or n.get("label", "?")
                provider = n.get("provider", "?")
                print(f"\n   [{pid}] {label} (provider={provider})")
            first_id = numbers[0].get("phone_number_id") or numbers[0].get("id")
            print(f"   -> Use ELEVENLABS_PHONE_NUMBER_ID={first_id}")
            return first_id
        else:
            print("NONE FOUND — connect a Twilio number in the ElevenLabs dashboard first.")
            return None
    except httpx.HTTPError as e:
        print(f"FAIL — {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Response: {e.response.text[:300]}")
        return None


def test_voices() -> None:
    """List available voices to confirm API access breadth."""
    print("5) Listing voices...", end=" ")
    try:
        r = httpx.get(f"{BASE}/v1/voices", headers=_headers(), timeout=15.0)
        r.raise_for_status()
        voices = r.json().get("voices", [])
        names = [v["name"] for v in voices[:5]]
        print(f"OK — {len(voices)} voices available (first 5: {names})")
    except httpx.HTTPError as e:
        print(f"FAIL — {e}")


def test_place_call(phone: str, phone_number_id: str) -> None:
    """Actually place an outbound test call."""
    print(f"\n6) Placing test call to {phone} (phone_number_id={phone_number_id})...")
    body: dict = {
        "agent_id": AGENT_ID,
        "agent_phone_number_id": phone_number_id,
        "to_number": phone,
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "attorney_name": "Michael Jennings",
                "firm_name": "Jennings & Associates LLC",
                "case_title": "Jennings v. Waste Management of NJ",
                "defendants": "Waste Management of NJ, Inc.",
                "lead_count": "47",
                "contact_email": "mjennings@jenningsassociates.com",
            },
        },
    }
    try:
        r = httpx.post(
            f"{BASE}/v1/convai/twilio/outbound-call",
            headers={**_headers(), "Content-Type": "application/json"},
            json=body,
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        conv_id = data.get("conversation_id", data)
        print(f"   Call placed! conversation_id={conv_id}")
        print("   Polling for result...")

        for i in range(12):
            time.sleep(10)
            poll = httpx.get(
                f"{BASE}/v1/convai/conversations/{conv_id}",
                headers=_headers(),
                timeout=15.0,
            )
            poll.raise_for_status()
            poll_data = poll.json()
            status = poll_data.get("status", "unknown")
            print(f"   [{i+1}/12] status={status}")

            if status in ("done", "failed", "ended"):
                transcript = poll_data.get("transcript", [])
                if transcript:
                    print("\n   --- TRANSCRIPT ---")
                    for turn in transcript:
                        role = turn.get("role", "?").upper()
                        msg = turn.get("message", "")
                        print(f"   {role}: {msg}")
                    print("   --- END ---")
                duration = poll_data.get("metadata", {}).get("call_duration_secs")
                if duration:
                    print(f"   Duration: {duration}s")
                break
        else:
            print("   Timed out waiting for call to complete (2 min). Check ElevenLabs dashboard.")
    except httpx.HTTPError as e:
        print(f"   FAIL — {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Response: {e.response.text[:500]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test ElevenLabs API connectivity")
    parser.add_argument("--call", metavar="PHONE", help="Place a live test call to this number (E.164 format)")
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: ELEVENLABS_API_KEY not set in environment or .env")
        sys.exit(1)

    print(f"ElevenLabs API key: {API_KEY[:8]}...{API_KEY[-4:]}")
    print(f"Agent ID: {AGENT_ID or '(not set)'}")
    print(f"Phone Number ID: {PHONE_NUMBER_ID or '(not set)'}")
    print()

    ok = test_api_key()
    if not ok:
        sys.exit(1)

    agent_data = test_agent()
    if agent_data:
        check_dynamic_vars(agent_data)

    discovered_phone_id = test_phone_numbers()
    test_voices()

    if args.call:
        if not AGENT_ID:
            print("\nERROR: ELEVENLABS_AGENT_ID required for placing calls")
            sys.exit(1)
        phone_id = PHONE_NUMBER_ID or discovered_phone_id
        if not phone_id:
            print("\nERROR: No phone number found. Set ELEVENLABS_PHONE_NUMBER_ID or connect Twilio in dashboard.")
            sys.exit(1)
        test_place_call(args.call, phone_id)

    print("\nDone.")


if __name__ == "__main__":
    main()
