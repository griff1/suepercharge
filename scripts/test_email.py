#!/usr/bin/env python
"""Send a test email to verify Resend integration works.

Usage:
    uv run scripts/test_email.py you@example.com
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from clients.ses_client import send_email  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run scripts/test_email.py <your-email>")
        print("\nNote: with onboarding@resend.dev, Resend only delivers to your account email.")
        sys.exit(1)

    to = sys.argv[1]
    msg_id = send_email(
        to=to,
        subject="Suepercharge test email",
        body_html=(
            "<div style='font-family:sans-serif;line-height:1.6;max-width:600px'>"
            "<h2>It works!</h2>"
            "<p>This is a test email from Suepercharge.</p>"
            "<p>If you see this in your inbox, Resend is configured correctly.</p>"
            "</div>"
        ),
        body_text="It works!\n\nThis is a test email from Suepercharge.\nResend is configured correctly.",
    )
    print(f"Sent! Message ID: {msg_id}")


if __name__ == "__main__":
    main()
