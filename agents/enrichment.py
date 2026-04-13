"""Enrichment agent — extracts filing attorney contacts from press releases.

Cadence: EventBridge cron, every 15 minutes.

Flow per invocation:
  1. Find Cases with status=parsed that have no Attorney row yet.
  2. Load the raw press release text from S3 (already stored by ingest agent).
  3. Ask Claude to extract the filing attorney's firm, name, email, phone, state.
  4. Compliance check: skip blocked states at launch (CA, NY, FL, NJ).
  5. Persist Attorney row with enriched_at timestamp.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

import compliance
import storage
from clients.anthropic_client import structured
from db import session
from models import Attorney, AttorneySource, Case, CaseStatus, ExtractedAttorney
from prompts import render

log = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

EXTRACT_MODEL = os.environ.get("ANTHROPIC_MODEL_FAST", "claude-haiku-4-5-20251001")


# ---------- Helpers ----------


def _cases_without_attorney(s: Session) -> list[Case]:
    """Cases that were successfully parsed but not yet enriched with attorney info."""
    return list(
        s.scalars(
            select(Case)
            .where(
                Case.status == CaseStatus.parsed,
                ~Case.id.in_(select(Attorney.case_id)),
            )
            .limit(20)
        ).all()
    )


def _load_press_release(case: Case) -> str | None:
    """Load the stored press release text. Returns None if not available."""
    if not case.raw_s3_key:
        return None
    try:
        return storage.get_text(case.raw_s3_key)
    except Exception:
        log.exception("failed to load press release for case %s (key=%s)", case.id, case.raw_s3_key)
        return None


def _extract_attorney(source_text: str) -> ExtractedAttorney:
    prompt = render("extract_attorney", source_text=source_text)
    return structured(
        prompt=prompt,
        response_model=ExtractedAttorney,
        model=EXTRACT_MODEL,
        temperature=0.0,
        max_tokens=512,
    )


def _persist_attorney(s: Session, case: Case, extracted: ExtractedAttorney) -> Attorney:
    attorney = Attorney(
        case_id=case.id,
        firm_name=extracted.firm_name,
        contact_name=extracted.contact_name,
        contact_email=extracted.contact_email,
        contact_phone=extracted.contact_phone,
        state_code=extracted.state_code,
        source=AttorneySource.press_release,
        outreach_blocked=False,
        enriched_at=datetime.now(UTC),
    )
    s.add(attorney)
    s.flush()
    return attorney


# ---------- Orchestration ----------


@dataclass
class EnrichmentResult:
    cases_checked: int
    enriched: int
    skipped_no_text: int
    skipped_blocked_state: int
    errored: int


def run_once() -> EnrichmentResult:
    result = EnrichmentResult(
        cases_checked=0,
        enriched=0,
        skipped_no_text=0,
        skipped_blocked_state=0,
        errored=0,
    )

    with session() as s:
        cases = _cases_without_attorney(s)
        result.cases_checked = len(cases)
        log.info("enrichment: %d cases to process", len(cases))

        for case in cases:
            try:
                text = _load_press_release(case)
                if not text:
                    log.warning("no press release text for case %s — skipping", case.id)
                    result.skipped_no_text += 1
                    continue

                extracted = _extract_attorney(text)

                if not compliance.outreach_state_is_allowed(extracted.state_code):
                    log.info(
                        "case %s attorney in blocked state %s — persisting but marking blocked",
                        case.id,
                        extracted.state_code,
                    )
                    # Still persist so we know we tried; blocked=True prevents outreach.
                    attorney = Attorney(
                        case_id=case.id,
                        firm_name=extracted.firm_name,
                        contact_name=extracted.contact_name,
                        contact_email=extracted.contact_email,
                        contact_phone=extracted.contact_phone,
                        state_code=extracted.state_code,
                        source=AttorneySource.press_release,
                        outreach_blocked=True,
                        enriched_at=datetime.now(UTC),
                    )
                    s.add(attorney)
                    s.flush()
                    result.skipped_blocked_state += 1
                    continue

                attorney = _persist_attorney(s, case, extracted)
                log.info(
                    "enriched case %s → attorney %s (%s) email=%s",
                    case.id,
                    attorney.id,
                    attorney.firm_name,
                    attorney.contact_email,
                )
                result.enriched += 1

            except Exception:
                log.exception("enrichment failed for case %s", case.id)
                result.errored += 1

    log.info("enrichment run: %s", result)
    return result


def handler(_event: dict, _context: object) -> dict:
    r = run_once()
    return {
        "cases_checked": r.cases_checked,
        "enriched": r.enriched,
        "skipped_no_text": r.skipped_no_text,
        "skipped_blocked_state": r.skipped_blocked_state,
        "errored": r.errored,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(handler({}, None), indent=2))
