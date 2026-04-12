#!/usr/bin/env -S uv run python
"""Dry-run one PR Newswire URL through the ingest pipeline.

No DB, no S3 — just prints the parsed case, the citation validation result,
and (optionally) the generated ICP. Useful for iterating on prompts without
waiting for a cron tick.

Usage:
  scripts/try_parse.py <press_release_url> [--with-icp] [--json]

Needs: ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project root importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.ingest import (
    build_icp,
    fetch_article_text,
    parse_case,
    validate_citations,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="PR Newswire article URL")
    ap.add_argument("--with-icp", action="store_true", help="Also run the ICP generator")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output")
    ap.add_argument("--text-file", help="Skip the fetch; read article text from this file instead")
    args = ap.parse_args()

    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read()
    else:
        print(f"[1/3] fetching {args.url}", file=sys.stderr)
        text = fetch_article_text(args.url)

    if not text.strip():
        print("Article body was empty after extraction.", file=sys.stderr)
        return 2
    print(f"      extracted {len(text)} chars", file=sys.stderr)

    print("[2/3] parsing with Claude…", file=sys.stderr)
    parsed = parse_case(text)

    ok, reason = validate_citations(parsed, text)

    icp = None
    if args.with_icp and parsed.is_viable_class_action and ok:
        print("[3/3] generating ICP…", file=sys.stderr)
        icp = build_icp(parsed)

    if args.json:
        out = {
            "parsed": parsed.model_dump(mode="json"),
            "citations_valid": ok,
            "citation_reason": reason,
            "icp": icp.model_dump(mode="json") if icp else None,
        }
        print(json.dumps(out, indent=2, default=str))
        return 0 if ok else 1

    # Human-readable
    print()
    print("=" * 72)
    print(f"Title:               {parsed.title}")
    print(f"Viable class action: {parsed.is_viable_class_action}")
    if parsed.reject_reason:
        print(f"Reject reason:       {parsed.reject_reason}")
    print(f"Defendants:          {', '.join(parsed.defendants) or '(none)'}")
    print(f"Product:             {parsed.product}")
    print(f"Harm type:           {parsed.harm_type}")
    print(f"Class period:        {parsed.class_period_start} → {parsed.class_period_end}")
    print(f"Geography:           {parsed.geography}")
    print(f"Payout range:        ${parsed.est_payout_low} - ${parsed.est_payout_high}")
    print(f"Deadline:            {parsed.deadline}")
    print(f"Citations:           {len(parsed.citations)}")
    for c in parsed.citations:
        print(f"  - {c.field:22s} [{c.start_offset}:{c.end_offset}] {c.quote[:60]!r}")
    print()
    print(f"Citation validation: {'OK' if ok else 'FAILED'}")
    if not ok:
        print(f"  reason: {reason}")

    if icp:
        print()
        print("-- ICP --")
        print(json.dumps(icp.model_dump(mode="json"), indent=2, default=str))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
