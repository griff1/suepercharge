#!/usr/bin/env python
"""Send a real pitch email with tracking and optionally place an ElevenLabs call.

No database required — this is the hackathon fast-path.

Usage:
    uv run scripts/send_pitch.py --to belleaitest@gmail.com
    uv run scripts/send_pitch.py --to belleaitest@gmail.com --call +15551234567
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import anthropic
from clients.ses_client import send_email, _inject_tracking
from clients import elevenlabs_client

HAIKU_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")

# --- Demo case data ---
CASE = {
    "title": "Johnson v. TechCorp Inc.",
    "defendants": "TechCorp Inc., TechCorp Holdings LLC",
    "deadline": "2026-06-15",
    "lead_count": 47,
}
ATTORNEY = {
    "name": "Sarah Mitchell",
    "firm": "Mitchell & Associates LLP",
}

B2B_DISCLOSURE = (
    "Suepercharge is an advertising and lead generation technology service, "
    "not a law firm and not a lawyer referral service. "
    "Fees are charged for marketing and advertising services."
)


def generate_email(case: dict, attorney: dict) -> tuple[str, str]:
    """Use Claude to generate the pitch email. Returns (subject, body_text)."""
    prompt = (
        "You are writing a B2B outreach email to a plaintiff-side class action attorney.\n"
        "Suepercharge ran digital advertising for their case and captured qualified leads.\n\n"
        "Lead with the NUMBER of leads and their eligibility quality.\n"
        "Tone: Direct, data-forward, respectful of the attorney's time.\n\n"
        f"- Attorney: {attorney['name']}\n"
        f"- Firm: {attorney['firm']}\n"
        f"- Case: {case['title']}\n"
        f"- Defendants: {case['defendants']}\n"
        f"- Leads available: {case['lead_count']}\n"
        f"- Deadline: {case['deadline']}\n\n"
        "Write subject and body separated by ---SUBJECT--- and ---BODY--- markers.\n"
        "Subject: under 60 chars. Body: 3 short paragraphs.\n"
        f"End body with exact text: '{B2B_DISCLOSURE}'"
    )

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
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
        subject = f"{case['lead_count']} qualified leads for {case['title'][:30]}"

    return subject, body


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a real Suepercharge pitch email")
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument("--call", default=None, help="Phone number to call after email (e.g. +15551234567)")
    parser.add_argument("--attorney-name", default=ATTORNEY["name"], help="Attorney name")
    parser.add_argument("--firm-name", default=ATTORNEY["firm"], help="Firm name")
    args = parser.parse_args()

    attorney = {"name": args.attorney_name, "firm": args.firm_name}
    attempt_id = str(uuid.uuid4())

    print(f"Generating email with Claude ({HAIKU_MODEL})...")
    subject, body_text = generate_email(CASE, attorney)

    print(f"\n--- Subject ---\n{subject}")
    print(f"\n--- Body ---\n{body_text}")

    html_body = (
        f"<div style='font-family:sans-serif;line-height:1.6;max-width:600px'>"
        + "".join(f"<p>{p.replace(chr(10), '<br/>')}</p>" for p in body_text.split("\n\n") if p.strip())
        + "</div>"
    )

    print(f"\nSending to {args.to}...")
    msg_id = send_email(
        to=args.to,
        subject=subject,
        body_html=html_body,
        body_text=body_text,
        attempt_id=attempt_id,
    )
    print(f"Email sent! Message ID: {msg_id}")
    print(f"Tracking attempt ID: {attempt_id}")

    if args.call:
        print(f"\nPlacing ElevenLabs call to {args.call}...")
        conversation_id = elevenlabs_client.place_call(
            phone_number=args.call,
            dynamic_variables={
                "attorney_name": attorney["name"],
                "firm_name": attorney["firm"],
                "case_title": CASE["title"],
                "defendants": CASE["defendants"],
                "lead_count": str(CASE["lead_count"]),
                "contact_email": args.to,
            },
        )
        print(f"Call placed! Conversation ID: {conversation_id}")

        print("\nPolling for call result...")
        result = elevenlabs_client.get_call_result(conversation_id)
        print(f"Status: {result.status}")
        if result.transcript:
            print(f"\n--- Transcript ---\n{result.transcript}")
    else:
        print("\nTo also place a call, re-run with: --call +15551234567")


if __name__ == "__main__":
    main()
