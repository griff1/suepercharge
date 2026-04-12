#!/usr/bin/env -S uv run python
"""List pending local approvals and let you approve/reject them.

The Slack stub writes pending approvals to `.local-approvals/<ts>.json`.
In `LOCAL_STUB_SLACK=manual` mode, the creative agent won't flip status
until a matching `<ts>.reaction` file appears. This script creates those.

Usage:
  scripts/approve.py                  # list pending approvals
  scripts/approve.py --approve <ts>
  scripts/approve.py --reject  <ts>
  scripts/approve.py --approve all    # approve every pending
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DIR = Path(".local-approvals")


def list_pending() -> list[Path]:
    if not DIR.exists():
        return []
    out = []
    for p in sorted(DIR.glob("local-*.json")):
        sibling = p.with_suffix(".reaction")
        if not sibling.exists():
            out.append(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--approve", metavar="TS")
    g.add_argument("--reject", metavar="TS")
    args = ap.parse_args()

    pending = list_pending()

    if args.approve or args.reject:
        verdict = "approve" if args.approve else "reject"
        target = args.approve or args.reject
        targets = [p.stem for p in pending] if target == "all" else [target]
        for ts in targets:
            (DIR / f"{ts}.reaction").write_text(verdict)
            print(f"{verdict:<8s} {ts}")
        print("(Next creative-agent tick will flip status)")
        return 0

    if not pending:
        print("No pending approvals. Creative agent is idle.")
        return 0

    print(f"{len(pending)} pending approval(s):")
    for p in pending:
        data = json.loads(p.read_text())
        print()
        print(f"  ts:       {data['ts']}")
        print(f"  mode:     {data['mode']}")
        print(f"  case:     {data['case_title']}")
        print(f"  headline: {data['headline']}")
        print(f"  body:     {data['primary_text'][:80]!r}")
        if data.get("image_url"):
            print(f"  image:    {data['image_url']}")
    print()
    print("Approve: scripts/approve.py --approve <ts>   (or: --approve all)")
    print("Reject:  scripts/approve.py --reject  <ts>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
