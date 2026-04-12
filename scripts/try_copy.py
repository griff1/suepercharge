#!/usr/bin/env -S uv run python
"""Iterate on the ad-copy prompt without re-paying for parse + ICP.

Reads the JSON output of `scripts/try_parse.py --with-icp --json` (from a
file or stdin) and runs JUST the copy generator. Perfect for tightening
the write_copy.j2 prompt against a frozen case + ICP.

Usage:
  scripts/try_parse.py <url> --with-icp --json > case.json
  scripts/try_copy.py case.json
  scripts/try_copy.py < case.json
  scripts/try_copy.py case.json --json                 # emit structured output
  scripts/try_copy.py case.json --iterations 3         # regenerate 3 times

Needs: ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import load_env  # noqa: F401 -- imports .env before ANTHROPIC_API_KEY lookup
from agents.creative import generate_compliant_variants
from models import GeneratedICP, ParsedCase


class _CaseShim:
    """Matches the attribute surface the copy prompt templates read."""

    def __init__(self, parsed: ParsedCase) -> None:
        self.title = parsed.title
        self.defendants = parsed.defendants
        self.product = parsed.product
        self.harm_type = parsed.harm_type
        self.class_period_start = _as_date(parsed.class_period_start)
        self.class_period_end = _as_date(parsed.class_period_end)
        self.geography = parsed.geography
        self.est_payout_low = parsed.est_payout_low
        self.est_payout_high = parsed.est_payout_high
        self.deadline = _as_date(parsed.deadline)


class _ICPShim:
    def __init__(self, icp: GeneratedICP) -> None:
        self.demographics = icp.demographics
        self.psychographics = icp.psychographics
        self.targeting_hints = icp.targeting_hints
        self.disqualifiers = icp.disqualifiers


def _as_date(value):
    if value is None or isinstance(value, date):
        return value
    # ISO strings round-trip from JSON
    return date.fromisoformat(value)


def _load_input(path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = sys.stdin.read()
    if not data.strip():
        print(
            "No input. Pass a JSON file path, or pipe in `try_parse.py --with-icp --json`.",
            file=sys.stderr,
        )
        sys.exit(2)
    return json.loads(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "input",
        nargs="?",
        help="JSON file with 'parsed' and 'icp' keys (try_parse.py --json output). Reads stdin if omitted.",
    )
    ap.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=1,
        help="Regenerate this many times to see variance (default: 1)",
    )
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    data = _load_input(args.input)
    parsed_raw = data.get("parsed")
    icp_raw = data.get("icp")
    if not parsed_raw or not icp_raw:
        print("Input JSON must have both 'parsed' and 'icp' keys.", file=sys.stderr)
        print("Tip: run `try_parse.py <url> --with-icp --json > case.json` first.", file=sys.stderr)
        return 2

    parsed = ParsedCase.model_validate(parsed_raw)
    icp = GeneratedICP.model_validate(icp_raw)
    case = _CaseShim(parsed)
    icp_shim = _ICPShim(icp)

    results: list[dict[str, Any]] = []
    for i in range(1, args.iterations + 1):
        if args.iterations > 1 and not args.json:
            print(f"\n=== iteration {i}/{args.iterations} ===", file=sys.stderr)
        result = generate_compliant_variants(case=case, icp=icp_shim)
        if result is None:
            record = {"ok": False, "reason": "copy_blocklist_failed_after_max_retries"}
        else:
            record = {
                "ok": True,
                "attempts": result.attempts,
                "variants": [v.model_dump(mode="json") for v in result.variants],
            }
        results.append(record)

        if not args.json:
            _print_result(i, record)

    if args.json:
        if args.iterations == 1:
            print(json.dumps(results[0], indent=2, default=str))
        else:
            print(json.dumps(results, indent=2, default=str))
    return 0 if all(r["ok"] for r in results) else 1


def _print_result(i: int, record: dict[str, Any]) -> None:
    if not record["ok"]:
        print(f"[{i}] FAILED: {record['reason']}")
        return
    print(f"[{i}] OK after {record['attempts']} attempt(s); {len(record['variants'])} variants:")
    for j, v in enumerate(record["variants"], 1):
        print(f"  ({j}) angle={v['angle']}  cta={v['cta']}")
        print(f"      headline: {v['headline']}")
        for line in v["primary_text"].splitlines():
            print(f"      body:     {line}")
        print(f"      why:      {v['rationale']}")


if __name__ == "__main__":
    sys.exit(main())
