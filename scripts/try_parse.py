#!/usr/bin/env -S uv run python
"""Dry-run one PR Newswire URL through the ingest pipeline.

No DB, no S3 — just prints the parsed case, the citation validation result,
and (optionally) the generated ICP and ad copy. Useful for iterating on
prompts without waiting for a cron tick.

Usage:
  scripts/try_parse.py <press_release_url>
  scripts/try_parse.py <url> --with-icp
  scripts/try_parse.py <url> --with-icp --with-copy
  scripts/try_parse.py <url> --json                  # machine-readable; pipe to try_copy.py
  scripts/try_parse.py --text-file path/to/pr.txt    # skip the fetch

Needs: ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make the project root importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import load_env  # noqa: F401 -- imports .env; must precede anything reading env vars
from agents.creative import generate_compliant_variants
from agents.ingest import (
    build_icp,
    fetch_article_text,
    parse_case,
    quote_in_source,
    validate_citations,
)
from clients import ideogram_client
from prompts import render as render_prompt


class _CaseShim:
    """Duck-types enough of the ORM Case model for the copy/ICP prompts.

    The prompt templates only touch attribute access on these fields, so we
    don't need a real Case row just to render."""

    def __init__(self, parsed):
        self.title = parsed.title
        self.defendants = parsed.defendants
        self.product = parsed.product
        self.harm_type = parsed.harm_type
        self.class_period_start = parsed.class_period_start
        self.class_period_end = parsed.class_period_end
        self.geography = parsed.geography
        self.est_payout_low = parsed.est_payout_low
        self.est_payout_high = parsed.est_payout_high
        self.deadline = parsed.deadline


class _ICPShim:
    def __init__(self, icp):
        self.demographics = icp.demographics
        self.psychographics = icp.psychographics
        self.targeting_hints = icp.targeting_hints
        self.disqualifiers = icp.disqualifiers


def _find_near_match(quote: str, source: str) -> str | None:
    """When a quote isn't verbatim in source, find the closest substring.

    Strategy: anchor on the first ~20 chars of the quote that look
    distinctive (letters + digits), search for them in the source, return
    a window around the hit. Good enough to eyeball Unicode/whitespace
    mismatches; not a general fuzzy match."""
    anchor = "".join(ch for ch in quote[:40] if ch.isalnum())[:12]
    if not anchor:
        return None
    idx = source.lower().find(anchor.lower())
    if idx < 0:
        return None
    start = max(0, idx - 10)
    end = min(len(source), idx + len(quote) + 10)
    return source[start:end]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", nargs="?", help="PR Newswire article URL")
    ap.add_argument("--with-icp", action="store_true", help="Also run the ICP generator")
    ap.add_argument(
        "--with-copy",
        action="store_true",
        help="Also run the copy generator (implies --with-icp)",
    )
    ap.add_argument(
        "--with-images",
        action="store_true",
        help="Also generate one image per variant (implies --with-copy). Writes PNGs to --image-dir.",
    )
    ap.add_argument(
        "--image-dir",
        default=".local-storage/try_parse",
        help="Directory to drop generated images into (default: .local-storage/try_parse)",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output")
    ap.add_argument("--text-file", help="Skip the fetch; read article text from this file instead")
    args = ap.parse_args()

    if not args.url and not args.text_file:
        ap.error("Need either a URL or --text-file")

    if args.with_images:
        args.with_copy = True
    if args.with_copy:
        args.with_icp = True

    if args.text_file:
        with open(args.text_file, encoding="utf-8") as f:
            text = f.read()
    else:
        print(f"[1/?] fetching {args.url}", file=sys.stderr)
        text = fetch_article_text(args.url)

    if not text.strip():
        print("Article body was empty after extraction.", file=sys.stderr)
        return 2
    print(f"      extracted {len(text)} chars", file=sys.stderr)

    print("[2/?] parsing with Claude…", file=sys.stderr)
    parsed = parse_case(text)
    ok, reason, _errors = validate_citations(parsed, text)

    icp = None
    variants: list = []
    if args.with_icp and parsed.is_viable_class_action and ok:
        print("[3/?] generating ICP…", file=sys.stderr)
        icp = build_icp(parsed, source_text=text)

    if args.with_copy and icp is not None:
        print("[4/?] generating 3 copy variants (informative / empathetic / urgent)…", file=sys.stderr)
        result = generate_compliant_variants(case=_CaseShim(parsed), icp=_ICPShim(icp))
        if result is None:
            print("      copy generation failed after MAX_COPY_RETRIES", file=sys.stderr)
        else:
            variants = result.variants
            print(f"      variants OK in {result.attempts} attempt(s)", file=sys.stderr)

    image_paths: list[str] = []
    if args.with_images and variants:
        from pathlib import Path

        out_dir = Path(args.image_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        case_shim = _CaseShim(parsed)
        icp_shim = _ICPShim(icp)
        for i, v in enumerate(variants, 1):
            print(f"[5/?] generating image for variant {i} ({v.angle})…", file=sys.stderr)
            image_prompt = render_prompt(
                "image_prompt", case=case_shim, icp=icp_shim, angle=v.angle
            )
            try:
                img_bytes = ideogram_client.generate(image_prompt, aspect_ratio="1x1")
                out_path = out_dir / f"variant-{i}-{v.angle}.png"
                out_path.write_bytes(img_bytes)
                image_paths.append(str(out_path))
                print(f"      wrote {out_path} ({len(img_bytes)} bytes)", file=sys.stderr)
            except Exception as e:
                print(f"      image generation failed: {e}", file=sys.stderr)
                image_paths.append("")

    if args.json:
        out = {
            "parsed": parsed.model_dump(mode="json"),
            "citations_valid": ok,
            "citation_reason": reason,
            "icp": icp.model_dump(mode="json") if icp else None,
            "variants": [v.model_dump(mode="json") for v in variants] if variants else None,
            "image_paths": image_paths or None,
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
    print(f"Class period:        {parsed.class_period_start} -> {parsed.class_period_end}")
    print(f"Geography:           {parsed.geography}")
    print(f"Payout range:        ${parsed.est_payout_low} - ${parsed.est_payout_high}")
    print(f"Deadline:            {parsed.deadline}")
    # Flag meanings match what the validator actually enforces:
    #   OK     — quote is at the claimed offsets verbatim
    #   ~      — quote is verbatim substring of source (but offsets wrong)
    #   ~norm  — quote matches source after Unicode/whitespace normalization
    #   MISS   — quote not found anywhere → would reject as hallucination
    print(f"Citations:           {len(parsed.citations)}")
    for c in parsed.citations:
        at_offsets = (
            0 <= c.start_offset < c.end_offset <= len(text)
            and text[c.start_offset : c.end_offset] == c.quote
        )
        if at_offsets:
            flag = "OK"
        elif c.quote in text:
            flag = "~"
        elif quote_in_source(c.quote, text):
            flag = "~norm"
        else:
            flag = "MISS"
        print(f"  [{flag:6s}] {c.field:22s} [{c.start_offset}:{c.end_offset}] ({len(c.quote)} chars)")
        print(f"           quote:  {c.quote!r}")
        if not at_offsets and 0 <= c.start_offset < c.end_offset <= len(text):
            at_off = text[c.start_offset : c.end_offset]
            print(f"           source: {at_off!r}")
        if flag == "MISS":
            hint = _find_near_match(c.quote, text)
            if hint:
                print(f"           near:   {hint!r}")

    print()
    print(f"Citation validation: {'OK' if ok else 'FAILED'}")
    if not ok:
        print(f"  reason: {reason}")

    if icp:
        print()
        print("-- ICP --")
        print(json.dumps(icp.model_dump(mode="json"), indent=2, default=str))

    if variants:
        for i, v in enumerate(variants, 1):
            print()
            print(f"-- VARIANT {i}/{len(variants)}: {v.angle.upper()} --")
            print(f"Headline:     {v.headline}")
            print(f"CTA:          {v.cta}")
            print("Primary text:")
            for line in v.primary_text.splitlines():
                print(f"  {line}")
            print(f"Rationale:    {v.rationale}")
            if image_paths and i <= len(image_paths) and image_paths[i - 1]:
                print(f"Image:        {image_paths[i - 1]}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
