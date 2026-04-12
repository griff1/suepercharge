#!/usr/bin/env -S uv run python
"""Insert a synthetic Case + ICP row into the local database.

Use this to exercise the creative and campaign agents without waiting for
PR Newswire to produce a real class-action post, and without spending Claude
tokens on parsing.

Usage:
  scripts/seed_case.py                        # insert a demo case with defaults
  scripts/seed_case.py --title "..." --product "Acme" --payout-low 50 --payout-high 500

Needs: local Postgres running (`make db-up && make migrate`).
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import session
from models import ICP, Case, CaseStatus


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--title", default="Acme data breach settlement (LOCAL SEED)")
    ap.add_argument("--defendant", default="Acme Inc.")
    ap.add_argument("--product", default="Acme Online Account")
    ap.add_argument("--harm-type", default="data breach")
    ap.add_argument("--geography", default="US")
    ap.add_argument("--payout-low", type=int, default=50)
    ap.add_argument("--payout-high", type=int, default=500)
    ap.add_argument("--class-start", type=date.fromisoformat, default=date(2020, 1, 1))
    ap.add_argument("--class-end", type=date.fromisoformat, default=date(2022, 12, 31))
    ap.add_argument("--deadline", type=date.fromisoformat, default=date(2027, 3, 15))
    args = ap.parse_args()

    # Make the URL unique per invocation so re-running doesn't hit the
    # UNIQUE constraint.
    url = f"https://local.seed/{uuid.uuid4()}"
    stamp = datetime.now(UTC).isoformat()

    with session() as s:
        case = Case(
            pr_newswire_url=url,
            title=args.title,
            defendants=[args.defendant],
            product=args.product,
            harm_type=args.harm_type,
            class_period_start=args.class_start,
            class_period_end=args.class_end,
            geography=args.geography,
            est_payout_low=args.payout_low,
            est_payout_high=args.payout_high,
            deadline=args.deadline,
            raw_s3_key=None,
            citations={"seeded": True, "at": stamp},
            status=CaseStatus.parsed,
            model_version="seed",
        )
        s.add(case)
        s.flush()

        icp = ICP(
            case_id=case.id,
            demographics={"age_range": "25-54", "geography": args.geography},
            psychographics={"traits": ["privacy-conscious", "online shopper"]},
            targeting_hints={"tone": "informative", "pain_points": ["account compromise"]},
            disqualifiers={"outside_class_period": True},
            model_version="seed",
        )
        s.add(icp)
        s.flush()

        print(f"Seeded case {case.id}")
        print(f"        icp {icp.id}")
        print(f"        url {url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
