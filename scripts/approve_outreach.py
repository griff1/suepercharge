#!/usr/bin/env python
"""Review and approve pending outreach actions (emails and calls).

Usage:
    uv run scripts/approve_outreach.py              # list all pending
    uv run scripts/approve_outreach.py <attempt_id> # approve one
    uv run scripts/approve_outreach.py --all        # approve everything
    uv run scripts/approve_outreach.py --reject <attempt_id>  # reject one
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APPROVAL_DIR = Path(".local-approvals/outreach")


def list_pending() -> list[Path]:
    if not APPROVAL_DIR.exists():
        return []
    return sorted(APPROVAL_DIR.glob("*.json"))


def show_pending() -> None:
    pending = list_pending()
    if not pending:
        print("No pending outreach actions.")
        return

    for p in pending:
        attempt_id = p.stem
        approved = (APPROVAL_DIR / f"{attempt_id}.approved").exists()
        data = json.loads(p.read_text())
        action = data.get("action", "?")
        to = data.get("to") or data.get("to_phone", "?")
        firm = data.get("firm", "?")
        subject = data.get("subject", "")

        status = "APPROVED" if approved else "PENDING"
        print(f"\n{'='*60}")
        print(f"  [{status}] {attempt_id}")
        print(f"  Action:  {action}")
        print(f"  To:      {to}")
        print(f"  Firm:    {firm}")
        if subject:
            print(f"  Subject: {subject}")
        if "text_body" in data:
            print(f"\n  --- Preview ---")
            for line in data["text_body"].split("\n"):
                print(f"  {line}")
    print(f"\n{'='*60}")
    print(f"\n{len(pending)} pending action(s). Approve with:")
    print(f"  python scripts/approve_outreach.py <attempt_id>")
    print(f"  python scripts/approve_outreach.py --all")


def approve(attempt_id: str) -> bool:
    review_file = APPROVAL_DIR / f"{attempt_id}.json"
    if not review_file.exists():
        print(f"No pending action found for {attempt_id}")
        return False

    sidecar = APPROVAL_DIR / f"{attempt_id}.approved"
    sidecar.write_text("approved")
    print(f"Approved: {attempt_id}")
    return True


def reject(attempt_id: str) -> bool:
    review_file = APPROVAL_DIR / f"{attempt_id}.json"
    if not review_file.exists():
        print(f"No pending action found for {attempt_id}")
        return False

    review_file.unlink()
    (APPROVAL_DIR / f"{attempt_id}.approved").unlink(missing_ok=True)
    print(f"Rejected and removed: {attempt_id}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Approve or reject pending outreach")
    parser.add_argument("attempt_id", nargs="?", help="Attempt ID to approve")
    parser.add_argument("--all", action="store_true", help="Approve all pending")
    parser.add_argument("--reject", metavar="ID", help="Reject an attempt")
    args = parser.parse_args()

    if args.reject:
        reject(args.reject)
    elif args.all:
        pending = list_pending()
        for p in pending:
            approve(p.stem)
        print(f"\nApproved {len(pending)} action(s). Run the outreach agent to execute.")
    elif args.attempt_id:
        approve(args.attempt_id)
        print("Run the outreach agent to execute the approved action.")
    else:
        show_pending()


if __name__ == "__main__":
    main()
