"""Ingest agent — pulls PR Newswire, parses class actions, builds ICPs.

Cadence: EventBridge cron, every 15 minutes (see infra/terraform/main.tf).

Flow per invocation:
  1. Fetch the configured PR Newswire RSS feed.
  2. Skip URLs we've already seen (Case.pr_newswire_url is UNIQUE).
  3. Fetch each new press release page, extract the article body.
  4. Upload raw text to S3 (provenance — we never republish it).
  5. Ask Claude to produce a ParsedCase with citations.
  6. Reject if the parser says it isn't a viable class action OR if any
     non-null field lacks a valid citation into the source (fail closed).
  7. Ask Claude to produce a GeneratedICP.
  8. Persist Case + ICP rows.

Parsing is the cheap step (Haiku); ICP generation is the judgment step (Opus).
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

import storage
from clients.anthropic_client import structured
from db import session
from models import (
    ICP,
    Case,
    CaseStatus,
    Citation,
    GeneratedICP,
    ParsedCase,
)
from prompts import render

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# PR Newswire's legal/law feed. Configurable via env so we can swap in a
# keyword-filtered search feed once we've measured precision.
DEFAULT_FEED_URL = (
    "https://www.prnewswire.com/rss/legal-law-public-policy-latest-news/"
    "legal-law-public-policy-latest-news-list.rss"
)

PARSE_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")
ICP_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")

# Filter keywords applied to RSS entry title/summary. A press release must
# match at least one to be fetched. Tight enough to skip obvious noise, loose
# enough that Claude sees the borderline cases.
CLASS_ACTION_KEYWORDS = (
    "class action",
    "class-action",
    "settlement",
    "plaintiff",
    "lawsuit filed",
    "complaint filed",
    "data breach",
    "consumer fraud",
)


@dataclass
class FeedEntry:
    url: str
    title: str
    summary: str


# ---------- External IO ----------


def fetch_feed(feed_url: str = DEFAULT_FEED_URL) -> list[FeedEntry]:
    """Parse the RSS feed and return entries. `feedparser` handles fetch + parse."""
    parsed = feedparser.parse(feed_url)
    entries: list[FeedEntry] = []
    for e in parsed.entries:
        url = getattr(e, "link", None)
        title = getattr(e, "title", "") or ""
        summary = getattr(e, "summary", "") or ""
        if not url:
            continue
        entries.append(FeedEntry(url=url, title=title, summary=summary))
    return entries


def looks_like_class_action(entry: FeedEntry) -> bool:
    blob = f"{entry.title}\n{entry.summary}".lower()
    return any(k in blob for k in CLASS_ACTION_KEYWORDS)


def fetch_article_text(url: str, *, client: httpx.Client | None = None) -> str:
    """Fetch a PR Newswire article and extract the body text.

    We deliberately keep extraction simple: grab everything inside the
    `release-body` container if present, else fall back to all visible text.
    If PRNW changes their DOM, the fallback still produces usable text."""
    close = False
    if client is None:
        client = httpx.Client(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": "suepercharge-ingest/0.1 (+https://suepercharge.ai)"},
        )
        close = True
    try:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text
    finally:
        if close:
            client.close()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()

    container = soup.find(class_="release-body") or soup.find("article") or soup.body
    if container is None:
        return ""
    text = container.get_text("\n", strip=True)
    # Collapse runs of blank lines.
    lines = [line for line in (ln.strip() for ln in text.splitlines()) if line]
    return "\n".join(lines)


def upload_raw(body: str, _source_url: str) -> str:
    """Store the raw extracted text. Key: raw/<uuid>.txt.

    Goes to S3 in prod, local filesystem in dev (see storage.py).
    """
    key = f"raw/{uuid.uuid4()}.txt"
    storage.put_text(key, body)
    return key


# ---------- Citation validation ----------


# Fields in ParsedCase that, if non-null, must have at least one matching citation.
_CITED_FIELDS = (
    "title",
    "defendants",
    "product",
    "harm_type",
    "class_period_start",
    "class_period_end",
    "geography",
    "est_payout_low",
    "est_payout_high",
    "deadline",
)


def validate_citations(parsed: ParsedCase, source_text: str) -> tuple[bool, str | None]:
    """Return (is_valid, reason). Every non-null cited field must have a citation
    whose quoted substring appears at the claimed offsets in source_text."""
    citations_by_field: dict[str, list[Citation]] = {}
    for c in parsed.citations:
        citations_by_field.setdefault(c.field, []).append(c)

    for field in _CITED_FIELDS:
        value = getattr(parsed, field)
        is_set = value not in (None, [], "")
        if not is_set:
            continue
        cites = citations_by_field.get(field, [])
        if not cites:
            return False, f"Missing citation for field: {field}"
        # Verify at least one citation's quote actually appears at its offsets.
        any_valid = False
        for c in cites:
            if 0 <= c.start_offset < c.end_offset <= len(source_text):
                actual = source_text[c.start_offset : c.end_offset]
                if actual == c.quote:
                    any_valid = True
                    break
        if not any_valid:
            return False, f"Citation offsets don't match source for field: {field}"

    return True, None


# ---------- Claude calls ----------


def parse_case(source_text: str) -> ParsedCase:
    prompt = render("parse_case", source_text=source_text)
    return structured(
        prompt=prompt,
        response_model=ParsedCase,
        model=PARSE_MODEL,
        temperature=0.0,  # deterministic extraction
        max_tokens=4096,
    )


def build_icp(parsed: ParsedCase) -> GeneratedICP:
    prompt = render("build_icp", case=parsed)
    return structured(
        prompt=prompt,
        response_model=GeneratedICP,
        model=ICP_MODEL,
        temperature=0.4,  # some creative latitude
        max_tokens=2048,
    )


# ---------- Persistence ----------


def _already_ingested(urls: list[str]) -> set[str]:
    if not urls:
        return set()
    with session() as s:
        rows = s.execute(select(Case.pr_newswire_url).where(Case.pr_newswire_url.in_(urls))).all()
    return {r[0] for r in rows}


def _persist_case_and_icp(
    *, entry: FeedEntry, parsed: ParsedCase, icp: GeneratedICP, raw_s3_key: str
) -> uuid.UUID:
    with session() as s:
        case = Case(
            pr_newswire_url=entry.url,
            title=parsed.title,
            defendants=parsed.defendants,
            product=parsed.product,
            harm_type=parsed.harm_type,
            class_period_start=parsed.class_period_start,
            class_period_end=parsed.class_period_end,
            geography=parsed.geography,
            est_payout_low=parsed.est_payout_low,
            est_payout_high=parsed.est_payout_high,
            deadline=parsed.deadline,
            raw_s3_key=raw_s3_key,
            citations={"citations": [c.model_dump() for c in parsed.citations]},
            status=CaseStatus.parsed,
            model_version=PARSE_MODEL,
        )
        s.add(case)
        s.flush()
        s.add(
            ICP(
                case_id=case.id,
                demographics=icp.demographics,
                psychographics=icp.psychographics,
                targeting_hints=icp.targeting_hints,
                disqualifiers=icp.disqualifiers,
                model_version=ICP_MODEL,
            )
        )
        return case.id


def _persist_rejected(*, entry: FeedEntry, reason: str, raw_s3_key: str | None) -> uuid.UUID:
    """Record the URL as seen-and-rejected so we don't re-parse on the next tick."""
    with session() as s:
        case = Case(
            pr_newswire_url=entry.url,
            title=entry.title[:500] or "(no title)",
            defendants=[],
            raw_s3_key=raw_s3_key,
            citations={},
            status=CaseStatus.rejected,
            reject_reason=reason,
            model_version=PARSE_MODEL,
        )
        s.add(case)
        s.flush()
        return case.id


# ---------- Orchestration ----------


@dataclass
class IngestResult:
    fetched: int
    skipped_existing: int
    skipped_keyword: int
    parsed: int
    rejected: int
    errored: int


def run_once(feed_url: str = DEFAULT_FEED_URL) -> IngestResult:
    entries = fetch_feed(feed_url)
    log.info("fetched %d entries from feed", len(entries))

    candidates = [e for e in entries if looks_like_class_action(e)]
    skipped_keyword = len(entries) - len(candidates)

    existing = _already_ingested([e.url for e in candidates])
    new = [e for e in candidates if e.url not in existing]
    log.info(
        "%d candidates after keyword filter; %d already ingested; %d new",
        len(candidates), len(existing), len(new),
    )

    result = IngestResult(
        fetched=len(entries),
        skipped_existing=len(existing),
        skipped_keyword=skipped_keyword,
        parsed=0,
        rejected=0,
        errored=0,
    )

    for entry in new:
        try:
            text = fetch_article_text(entry.url)
            if not text.strip():
                _persist_rejected(entry=entry, reason="empty article body", raw_s3_key=None)
                result.rejected += 1
                continue

            s3_key = upload_raw(text, entry.url)
            parsed = parse_case(text)

            if not parsed.is_viable_class_action:
                _persist_rejected(
                    entry=entry,
                    reason=parsed.reject_reason or "not a viable class action",
                    raw_s3_key=s3_key,
                )
                result.rejected += 1
                continue

            ok, reason = validate_citations(parsed, text)
            if not ok:
                _persist_rejected(entry=entry, reason=f"citation check failed: {reason}", raw_s3_key=s3_key)
                result.rejected += 1
                continue

            icp = build_icp(parsed)
            case_id = _persist_case_and_icp(entry=entry, parsed=parsed, icp=icp, raw_s3_key=s3_key)
            log.info("ingested case %s from %s", case_id, entry.url)
            result.parsed += 1

        except Exception:
            log.exception("ingest failed for %s", entry.url)
            result.errored += 1

    log.info("ingest run: %s", result)
    return result


# ---------- Lambda + CLI entrypoints ----------


def handler(event: dict, _context: object) -> dict:
    feed_url = event.get("feed_url") or os.environ.get("PRNW_FEED_URL", DEFAULT_FEED_URL)
    result = run_once(feed_url)
    return {
        "fetched": result.fetched,
        "skipped_existing": result.skipped_existing,
        "skipped_keyword": result.skipped_keyword,
        "parsed": result.parsed,
        "rejected": result.rejected,
        "errored": result.errored,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(handler({}, None), indent=2))
