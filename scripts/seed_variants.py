#!/usr/bin/env python
"""Seed the initial A/B prompt variants into the database.

Run once after `make migrate`. Safe to re-run — skips existing rows.

Experiments seeded:
  - email_subject_body   v1, v2  — two email angles
  - call_script          v1, v2  — two call approaches
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select

from db import session
from models import PromptVariant

VARIANTS: list[dict] = [
    # ── email_subject_body ──────────────────────────────────────────────
    {
        "experiment_key": "email_subject_body",
        "variant_id": "v1",
        "content": (
            "You are writing a B2B outreach email to a plaintiff-side class action attorney.\n"
            "Suepercharge ran digital advertising for their case and captured qualified leads.\n\n"
            "Angle: Lead with the NUMBER of leads and their eligibility quality.\n"
            "Tone: Direct, data-forward, respectful of the attorney's time.\n"
            "Open with the lead count and case name in the first sentence.\n\n"
            "Context:\n"
            "- Attorney: {{ attorney_name }}\n"
            "- Firm: {{ firm_name }}\n"
            "- Case: {{ case_title }}\n"
            "- Defendants: {{ defendants }}\n"
            "- Leads available: {{ lead_count }}\n"
            "- Deadline: {{ deadline }}\n\n"
            "Write subject and body separated by ---SUBJECT--- and ---BODY--- markers.\n"
            "Subject: under 60 chars. Body: 3 short paragraphs + compliance disclosure.\n"
            "End body with exact text: 'Suepercharge is an advertising and lead generation "
            "technology service, not a law firm and not a lawyer referral service. "
            "Fees are charged for marketing and advertising services.'"
        ),
    },
    {
        "experiment_key": "email_subject_body",
        "variant_id": "v2",
        "content": (
            "You are writing a B2B outreach email to a plaintiff-side class action attorney.\n"
            "Suepercharge ran digital advertising for their case and captured qualified leads.\n\n"
            "Angle: Lead with URGENCY — the case deadline is approaching and leads are perishable.\n"
            "Tone: Collegial, time-sensitive.\n"
            "Reference the specific defendant to show we understand the case.\n\n"
            "Context:\n"
            "- Attorney: {{ attorney_name }}\n"
            "- Firm: {{ firm_name }}\n"
            "- Case: {{ case_title }}\n"
            "- Defendants: {{ defendants }}\n"
            "- Leads available: {{ lead_count }}\n"
            "- Deadline: {{ deadline }}\n\n"
            "Write subject and body separated by ---SUBJECT--- and ---BODY--- markers.\n"
            "Subject: under 60 chars. Body: 3 short paragraphs + compliance disclosure.\n"
            "End body with exact text: 'Suepercharge is an advertising and lead generation "
            "technology service, not a law firm and not a lawyer referral service. "
            "Fees are charged for marketing and advertising services.'"
        ),
    },
    # ── call_script ─────────────────────────────────────────────────────
    {
        "experiment_key": "call_script",
        "variant_id": "v1",
        "content": (
            "Opening style: DIRECT — get to the point in under 30 seconds.\n"
            "After the mandatory AI disclosure, immediately state the case name and lead count.\n"
            "Do not use small talk.\n\n"
            "Case: {{ case_title }}\n"
            "Firm: {{ firm_name }}\n"
            "Attorney: {{ attorney_name }}\n"
            "Defendants: {{ defendants }}\n"
            "Leads: {{ lead_count }}\n"
            "Email: {{ contact_email }}\n\n"
            "Script: [AI disclosure] -> case + leads -> consent details -> confirm email -> close."
        ),
    },
    {
        "experiment_key": "call_script",
        "variant_id": "v2",
        "content": (
            "Opening style: QUESTION-FIRST — open with a yes/no question.\n"
            "After the mandatory AI disclosure, ask: 'Are you still looking for "
            "plaintiffs for the [case] case?'\n"
            "This filters disinterested attorneys immediately.\n\n"
            "Case: {{ case_title }}\n"
            "Firm: {{ firm_name }}\n"
            "Attorney: {{ attorney_name }}\n"
            "Defendants: {{ defendants }}\n"
            "Leads: {{ lead_count }}\n"
            "Email: {{ contact_email }}\n\n"
            "Script: [AI disclosure] -> qualifying question -> if yes: leads + email -> close."
        ),
    },
]


def main() -> None:
    inserted = 0
    skipped = 0

    with session() as s:
        for v in VARIANTS:
            exists = s.scalars(
                select(PromptVariant).where(
                    PromptVariant.experiment_key == v["experiment_key"],
                    PromptVariant.variant_id == v["variant_id"],
                )
            ).first()
            if exists:
                print(f"  skip  {v['experiment_key']}/{v['variant_id']}")
                skipped += 1
                continue
            s.add(PromptVariant(
                experiment_key=v["experiment_key"],
                variant_id=v["variant_id"],
                content=v["content"],
                is_active=True,
                weight=1.0,
            ))
            s.flush()
            print(f"  insert {v['experiment_key']}/{v['variant_id']}")
            inserted += 1

    print(f"\nDone: {inserted} inserted, {skipped} skipped.")


if __name__ == "__main__":
    main()
