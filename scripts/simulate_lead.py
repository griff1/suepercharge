#!/usr/bin/env -S uv run python
"""Simulate a Meta leadgen webhook hitting our handler — no HTTP server, no
Meta account required. Directly invokes `agents.campaign.webhook_handler`
with a correctly signed event payload.

Flow:
  1. Finds a local Campaign row (or uses --campaign-id), grabs its lead_form_id.
  2. Writes a fake lead payload to .local-leads/<leadgen_id>.json so the stub
     `meta_client.fetch_lead` returns it.
  3. Crafts a signed Meta webhook event and calls webhook_handler directly.
  4. Prints the handler response and the new Lead row.

Usage:
  scripts/simulate_lead.py                      # uses the most recent campaign
  scripts/simulate_lead.py --campaign-id <uuid>

Needs: META_APP_SECRET in env (for signing), local DB.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

from agents import campaign as campaign_agent
from db import session
from models import Campaign, Lead


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign-id", help="Campaign UUID (defaults to the most recent)")
    ap.add_argument("--name", default="Jane Local")
    ap.add_argument("--email", default="jane@local.test")
    ap.add_argument("--phone", default="+15555550123")
    ap.add_argument("--qualifying", default="Yes", choices=["Yes", "No"])
    args = ap.parse_args()

    secret = os.environ.get("META_APP_SECRET")
    if not secret:
        # Stub mode: skip signature check by setting a secret just for this run.
        secret = "local-dev-secret"
        os.environ["META_APP_SECRET"] = secret

    # 1. Find a campaign with a lead_form_id.
    with session() as s:
        q = select(Campaign).where(Campaign.lead_form_id.is_not(None))
        if args.campaign_id:
            q = q.where(Campaign.id == uuid.UUID(args.campaign_id))
        else:
            q = q.order_by(Campaign.created_at.desc())
        campaign = s.scalars(q).first()
        if campaign is None:
            print("No campaign with a lead_form_id exists. Deploy one first "
                  "(try `make campaign` after `make creative` approves one).", file=sys.stderr)
            return 2
        form_id = campaign.lead_form_id
        campaign_uuid = campaign.id

    # 2. Drop a fake lead payload for the meta_client stub to read.
    leadgen_id = f"local-lead-{uuid.uuid4().hex[:12]}"
    leads_dir = Path(os.environ.get("LOCAL_LEADS_DIR", ".local-leads"))
    leads_dir.mkdir(parents=True, exist_ok=True)
    (leads_dir / f"{leadgen_id}.json").write_text(json.dumps({
        "id": leadgen_id,
        "created_time": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+0000"),
        "field_data": [
            {"name": "full_name", "values": [args.name]},
            {"name": "email", "values": [args.email]},
            {"name": "phone_number", "values": [args.phone]},
            {"name": "qualifying_0", "values": [args.qualifying]},
        ],
    }))

    # 3. Craft a signed Meta webhook event and invoke the handler.
    body_obj = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "leadgen_id": leadgen_id,
                            "form_id": form_id,
                            "created_time": datetime.now(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%S+0000"
                            ),
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(body_obj)
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"x-hub-signature-256": f"sha256={sig}"},
        "body": body,
    }

    resp = campaign_agent.webhook_handler(event, None)
    print("handler response:", json.dumps(resp, indent=2))

    # 4. Print the new Lead row.
    with session() as s:
        lead = s.scalars(
            select(Lead).where(Lead.meta_lead_id == leadgen_id)
        ).first()
        if lead is None:
            print("No Lead row created. Check handler response above.", file=sys.stderr)
            return 1
        print()
        print(f"Lead {lead.id} created under campaign {campaign_uuid}")
        print(f"  meta_lead_id:         {lead.meta_lead_id}")
        print(f"  consent_text_version: {lead.consent_text_version}")
        print(f"  consent_ts:           {lead.consent_ts}")
        print(f"  name (decrypted):     {(lead.name_enc or b'').decode() or '(none)'}")
        print(f"  email (decrypted):    {(lead.email_enc or b'').decode() or '(none)'}")
        print(f"  phone (decrypted):    {(lead.phone_enc or b'').decode() or '(none)'}")
        print(f"  intake_answers:       {lead.intake_answers}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
