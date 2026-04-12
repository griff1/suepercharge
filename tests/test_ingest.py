"""Ingest agent — unit tests for the pure logic (no network).

Citation validation is the load-bearing safety check: if this passes bad data,
we publish hallucinated facts in ad copy. Any test added here should represent
a real failure mode we want caught before launch.
"""
from __future__ import annotations

from datetime import date

from agents.ingest import (
    CLASS_ACTION_KEYWORDS,
    FeedEntry,
    looks_like_class_action,
    validate_citations,
)
from models import Citation, ParsedCase

SOURCE = (
    "Acme Inc. has agreed to a class action settlement. The class period is "
    "January 1, 2020 to December 31, 2022. Claimants may receive between "
    "$50 and $500. Deadline to file: March 15, 2027."
)


def _find(sub: str) -> tuple[int, int]:
    i = SOURCE.index(sub)
    return i, i + len(sub)


def _parsed(**overrides) -> ParsedCase:
    defaults = dict(
        title="Acme Class Action Settlement",
        defendants=["Acme Inc."],
        product=None,
        harm_type=None,
        class_period_start=date(2020, 1, 1),
        class_period_end=date(2022, 12, 31),
        geography=None,
        est_payout_low=50,
        est_payout_high=500,
        deadline=date(2027, 3, 15),
        is_viable_class_action=True,
        reject_reason=None,
        citations=[],
    )
    defaults.update(overrides)
    return ParsedCase(**defaults)


def _cite(field: str, quote: str) -> Citation:
    start, end = _find(quote)
    return Citation(field=field, quote=quote, start_offset=start, end_offset=end)


# ---------- Citation validation ----------


def test_valid_citations_pass() -> None:
    parsed = _parsed(
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            _cite("defendants", "Acme Inc."),
            _cite("class_period_start", "January 1, 2020"),
            _cite("class_period_end", "December 31, 2022"),
            _cite("est_payout_low", "$50"),
            _cite("est_payout_high", "$500"),
            _cite("deadline", "March 15, 2027"),
        ]
    )
    ok, reason = validate_citations(parsed, SOURCE)
    assert ok, reason


def test_missing_citation_for_non_null_field_rejected() -> None:
    parsed = _parsed(
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            _cite("defendants", "Acme Inc."),
            # Missing citations for dates + payouts.
        ]
    )
    ok, reason = validate_citations(parsed, SOURCE)
    assert not ok
    assert "class_period_start" in reason  # first missing field reported


def test_wrong_offsets_rejected() -> None:
    # Quote matches, but offsets point at wrong region.
    parsed = _parsed(
        title="Acme Class Action Settlement",
        defendants=["Acme Inc."],
        class_period_start=None,
        class_period_end=None,
        est_payout_low=None,
        est_payout_high=None,
        deadline=None,
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            Citation(field="defendants", quote="Acme Inc.", start_offset=0, end_offset=5),
        ],
    )
    ok, reason = validate_citations(parsed, SOURCE)
    assert not ok
    assert "defendants" in reason


def test_null_fields_need_no_citation() -> None:
    parsed = _parsed(
        product=None,
        harm_type=None,
        geography=None,
        est_payout_low=None,
        est_payout_high=None,
        deadline=None,
        class_period_start=None,
        class_period_end=None,
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            _cite("defendants", "Acme Inc."),
        ],
    )
    ok, reason = validate_citations(parsed, SOURCE)
    assert ok, reason


def test_fabricated_quote_rejected() -> None:
    # Claude invents a quote that isn't in the source.
    parsed = _parsed(
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            _cite("defendants", "Acme Inc."),
            _cite("class_period_start", "January 1, 2020"),
            _cite("class_period_end", "December 31, 2022"),
            _cite("est_payout_low", "$50"),
            _cite("est_payout_high", "$500"),
            Citation(  # offsets reference a region that doesn't match
                field="deadline",
                quote="April 1, 2099",
                start_offset=0,
                end_offset=len("April 1, 2099"),
            ),
        ]
    )
    ok, reason = validate_citations(parsed, SOURCE)
    assert not ok
    assert "deadline" in reason


# ---------- Keyword filter ----------


def test_keyword_filter_accepts_class_action_titles() -> None:
    e = FeedEntry(url="x", title="Class action filed against Acme", summary="")
    assert looks_like_class_action(e)


def test_keyword_filter_rejects_unrelated() -> None:
    e = FeedEntry(url="x", title="Acme announces Q3 earnings", summary="Record quarter")
    assert not looks_like_class_action(e)


def test_keyword_filter_covers_the_intended_keywords() -> None:
    # Any change to this list is a deliberate scope change; force it through review.
    assert set(CLASS_ACTION_KEYWORDS) == {
        "class action",
        "class-action",
        "settlement",
        "plaintiff",
        "lawsuit filed",
        "complaint filed",
        "data breach",
        "consumer fraud",
    }
