"""Handoff layer: lets the ad copy agent read parsed cases and claim them."""
from __future__ import annotations

import uuid

from sqlalchemy import select, update

from db import session
from models import ICP, Case, CaseStatus


def get_ready_cases() -> list[dict]:
    """Return all parsed cases with their ICPs, ready for ad copy generation."""
    with session() as s:
        rows = s.execute(
            select(Case, ICP).join(ICP, ICP.case_id == Case.id).where(Case.status == CaseStatus.parsed)
        ).all()

    results = []
    for case, icp in rows:
        results.append({
            "case_id": str(case.id),
            "title": case.title,
            "defendants": case.defendants,
            "product": case.product,
            "harm_type": case.harm_type,
            "geography": case.geography,
            "deadline": str(case.deadline) if case.deadline else None,
            "payout_low": case.est_payout_low,
            "payout_high": case.est_payout_high,
            "source_url": case.pr_newswire_url,
            "law_firm_contact": case.law_firm_contact,
            "demographics": icp.demographics,
            "psychographics": icp.psychographics,
            "targeting_hints": icp.targeting_hints,
            "disqualifiers": icp.disqualifiers,
        })
    return results


def mark_status(case_id: str | uuid.UUID, status: str) -> None:
    """Update a case's status (e.g. 'parsed' -> 'ads_running')."""
    with session() as s:
        s.execute(update(Case).where(Case.id == uuid.UUID(str(case_id))).values(status=status))
