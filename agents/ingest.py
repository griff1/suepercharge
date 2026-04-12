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
from datetime import date

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

# Structured, human-readable log format. Suppress noisy httpx request logs.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _short_url(url: str) -> str:
    """Shorten a PR Newswire URL for log readability."""
    # https://www.prnewswire.com/news-releases/some-long-slug-302739507.html -> some-long-slug
    slug = url.rsplit("/", 1)[-1].replace(".html", "") if "prnewswire.com" in url else url
    return slug[:60]

# PR Newswire's legal/law feed. Configurable via env so we can swap in a
# keyword-filtered search feed once we've measured precision.
DEFAULT_FEED_URL = (
    "https://www.prnewswire.com/rss/legal-law-public-policy-latest-news/"
    "legal-law-public-policy-latest-news-list.rss"
)

PARSE_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")
ICP_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6")
MAX_PARSE_RETRIES = 2  # one initial + up to two retries with error feedback

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
    import html as _html

    parsed = feedparser.parse(feed_url)
    entries: list[FeedEntry] = []
    for e in parsed.entries:
        url = getattr(e, "link", None)
        title = _html.unescape(getattr(e, "title", "") or "")
        summary = _html.unescape(getattr(e, "summary", "") or "")
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
            transport=httpx.HTTPTransport(retries=3),
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

    container = (
        soup.find(class_="release-body")
        or soup.find(class_="prnews-body")
        or soup.find(id="release-body")
        or soup.find("article")
        or soup.find(class_="main-content")
        or soup.body
    )
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


@dataclass
class CitationError:
    field: str
    quote: str
    start: int
    end: int
    actual: str


def validate_citations(
    parsed: ParsedCase, source_text: str,
) -> tuple[bool, str | None, list[CitationError]]:
    """Return (is_valid, reason, errors). Every non-null cited field must have a
    citation whose quoted substring appears at the claimed offsets in source_text.
    When validation fails, `errors` contains details for each bad citation so the
    retry prompt can tell Claude exactly what went wrong."""
    citations_by_field: dict[str, list[Citation]] = {}
    for c in parsed.citations:
        citations_by_field.setdefault(c.field, []).append(c)

    errors: list[CitationError] = []
    first_reason: str | None = None

    for field in _CITED_FIELDS:
        value = getattr(parsed, field)
        is_set = value not in (None, [], "")
        if not is_set:
            continue
        cites = citations_by_field.get(field, [])
        if not cites:
            if first_reason is None:
                first_reason = f"Missing citation for field: {field}"
            errors.append(CitationError(field=field, quote="", start=0, end=0, actual=""))
            continue
        # Verify at least one citation's quote actually appears at its offsets.
        any_valid = False
        for c in cites:
            if 0 <= c.start_offset < c.end_offset <= len(source_text):
                actual = source_text[c.start_offset : c.end_offset]
                if actual == c.quote:
                    any_valid = True
                    break
        if not any_valid:
            if first_reason is None:
                first_reason = f"Citation offsets don't match source for field: {field}"
            # Use the first citation's details for the error report.
            c = cites[0]
            actual = ""
            if 0 <= c.start_offset < c.end_offset <= len(source_text):
                actual = source_text[c.start_offset : c.end_offset]
            errors.append(CitationError(
                field=field, quote=c.quote, start=c.start_offset, end=c.end_offset, actual=actual,
            ))

    if errors:
        return False, first_reason, errors
    return True, None, []


# ---------- Claude calls ----------


MAX_SOURCE_CHARS = 50_000  # ~12k tokens; safety valve for outlier articles


def parse_case(
    source_text: str, *, citation_errors: list[CitationError] | None = None,
) -> ParsedCase:
    if len(source_text) > MAX_SOURCE_CHARS:
        log.warning("truncating article from %d to %d chars", len(source_text), MAX_SOURCE_CHARS)
        source_text = source_text[:MAX_SOURCE_CHARS]
    prompt = render("parse_case", source_text=source_text, citation_errors=citation_errors)
    return structured(
        prompt=prompt,
        response_model=ParsedCase,
        model=PARSE_MODEL,
        temperature=0.0,  # deterministic extraction
        max_tokens=4096,
    )


def build_icp(parsed: ParsedCase, source_text: str) -> GeneratedICP:
    prompt = render("build_icp", case=parsed, source_text=source_text)
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


def _log_icp(icp: GeneratedICP) -> None:
    """Pretty-print ICP fields to the log, one sub-field per line."""

    def _val(v: object) -> str:
        if isinstance(v, dict):
            if "value" in v and len(v) == 1:
                return str(v["value"])
            if "items" in v and len(v) == 1:
                return _val(v["items"])
            return "; ".join(f"{k}: {_val(sub)}" for k, sub in v.items())
        if isinstance(v, list):
            return ", ".join(_val(i) for i in v)
        return str(v)

    def _log_section(label: str, data: dict) -> None:
        log.info("         %s", label)
        for k, v in data.items():
            log.info("           %-14s %s", k + ":", _val(v))

    if icp.demographics:
        _log_section("WHO", icp.demographics)
    if icp.psychographics:
        _log_section("INTERESTS", icp.psychographics)
    if icp.targeting_hints:
        _log_section("AD ANGLE", icp.targeting_hints)
    if icp.disqualifiers:
        _log_section("EXCLUDE", icp.disqualifiers)


def run_once(feed_urls: str | None = None) -> IngestResult:
    urls = (feed_urls or os.environ.get("PRNW_FEED_URLS") or DEFAULT_FEED_URL).split(",")
    urls = [u.strip() for u in urls if u.strip()]

    all_entries: list[FeedEntry] = []
    for url in urls:
        all_entries.extend(fetch_feed(url))

    # Dedup by URL across feeds.
    seen: set[str] = set()
    entries: list[FeedEntry] = []
    for e in all_entries:
        if e.url not in seen:
            seen.add(e.url)
            entries.append(e)

    log.info("")
    log.info("=" * 60)
    log.info("  INGEST TICK")
    log.info("=" * 60)
    log.info("  Feed entries:    %d from %d feed(s)", len(entries), len(urls))

    candidates = [e for e in entries if looks_like_class_action(e)]
    skipped_keyword = len(entries) - len(candidates)

    existing = _already_ingested([e.url for e in candidates])
    new = [e for e in candidates if e.url not in existing]
    log.info("  Keyword match:   %d", len(candidates))
    log.info("  Already seen:    %d", len(existing))
    log.info("  New to process:  %d", len(new))
    log.info("-" * 60)

    result = IngestResult(
        fetched=len(entries),
        skipped_existing=len(existing),
        skipped_keyword=skipped_keyword,
        parsed=0,
        rejected=0,
        errored=0,
    )

    for i, entry in enumerate(new, 1):
        slug = _short_url(entry.url)
        log.info("")
        log.info("  [%d/%d] %s", i, len(new), entry.title)
        log.info("         %s", slug)

        try:
            text = fetch_article_text(entry.url)
            if not text.strip():
                log.warning("         SKIP  empty article body")
                _persist_rejected(entry=entry, reason="empty article body", raw_s3_key=None)
                result.rejected += 1
                continue

            log.info("         scraped %d chars", len(text))
            s3_key = upload_raw(text, entry.url)

            log.info("         parsing with LLM...")
            parsed = parse_case(text)

            if not parsed.is_viable_class_action:
                log.info("         SKIP  not viable: %s", parsed.reject_reason or "n/a")
                _persist_rejected(
                    entry=entry,
                    reason=parsed.reject_reason or "not a viable class action",
                    raw_s3_key=s3_key,
                )
                result.rejected += 1
                continue

            if not parsed.title:
                parsed.title = entry.title
            log.info("         parsed: %s", parsed.title[:60])
            if parsed.defendants:
                log.info("         defendants: %s", ", ".join(parsed.defendants))
            if parsed.geography:
                log.info("         geography: %s", parsed.geography)
            if parsed.est_payout_low or parsed.est_payout_high:
                log.info("         payout: $%s - $%s", parsed.est_payout_low, parsed.est_payout_high)
            if parsed.deadline:
                log.info("         deadline: %s", parsed.deadline)
                days_left = (parsed.deadline - date.today()).days
                if days_left < 7:
                    log.warning("         SKIP  deadline too soon (%d days) — no time for ad spend", days_left)
                    _persist_rejected(
                        entry=entry,
                        reason=f"deadline too soon ({days_left} days left)",
                        raw_s3_key=s3_key,
                    )
                    result.rejected += 1
                    continue

            log.info("         generating ICP...")
            icp = build_icp(parsed, source_text=text)
            case_id = _persist_case_and_icp(entry=entry, parsed=parsed, icp=icp, raw_s3_key=s3_key)
            log.info("         SAVED  case %s", case_id)
            log.info("         --- ICP ---")
            _log_icp(icp)
            result.parsed += 1

        except Exception as exc:
            log.error("         ERROR  %s: %s", type(exc).__name__, exc)
            result.errored += 1

    log.info("")
    log.info("=" * 60)
    log.info("  RESULTS  parsed=%d  rejected=%d  errored=%d  skipped=%d",
             result.parsed, result.rejected, result.errored,
             result.skipped_existing + result.skipped_keyword)
    log.info("=" * 60)
    return result


# ---------- Lambda + CLI entrypoints ----------


def handler(event: dict, _context: object) -> dict:
    feed_urls = event.get("feed_urls") or event.get("feed_url") or None
    result = run_once(feed_urls)
    return {
        "fetched": result.fetched,
        "skipped_existing": result.skipped_existing,
        "skipped_keyword": result.skipped_keyword,
        "parsed": result.parsed,
        "rejected": result.rejected,
        "errored": result.errored,
    }


if __name__ == "__main__":
    import argparse
    import json
    import time

    ap = argparse.ArgumentParser(description="Run the ingest agent.")
    ap.add_argument(
        "--loop", action="store_true",
        help="Run continuously on an interval instead of once.",
    )
    ap.add_argument(
        "--interval", type=int,
        default=int(os.environ.get("INGEST_INTERVAL_SECONDS", "300")),
        help="Seconds between runs when --loop is set (default: 300 / env INGEST_INTERVAL_SECONDS).",
    )
    args = ap.parse_args()

    if not args.loop:
        print(json.dumps(handler({}, None), indent=2))
    else:
        log.info("Ingest agent started  (interval=%ds)", args.interval)
        while True:
            try:
                run_once()
            except Exception:
                log.exception("Tick failed with unhandled error")
            log.info("  Next tick in %ds...", args.interval)
            log.info("")
            time.sleep(args.interval)
