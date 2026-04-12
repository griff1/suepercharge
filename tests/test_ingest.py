"""Ingest agent — unit tests for the pure logic (no network).

Citation validation is the load-bearing safety check: if this passes bad data,
we publish hallucinated facts in ad copy. Any test added here should represent
a real failure mode we want caught before launch.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from agents.ingest import (
    CLASS_ACTION_KEYWORDS,
    CitationError,
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


ALL_VALID_CITATIONS = [
    _cite("title", "Acme Inc. has agreed to a class action settlement"),
    _cite("defendants", "Acme Inc."),
    _cite("class_period_start", "January 1, 2020"),
    _cite("class_period_end", "December 31, 2022"),
    _cite("est_payout_low", "$50"),
    _cite("est_payout_high", "$500"),
    _cite("deadline", "March 15, 2027"),
]


# ---------- Citation validation ----------


def test_valid_citations_pass() -> None:
    parsed = _parsed(citations=ALL_VALID_CITATIONS)
    ok, reason, errors = validate_citations(parsed, SOURCE)
    assert ok, reason
    assert errors == []


def test_missing_citation_for_non_null_field_rejected() -> None:
    parsed = _parsed(
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            _cite("defendants", "Acme Inc."),
            # Missing citations for dates + payouts.
        ]
    )
    ok, reason, _errors = validate_citations(parsed, SOURCE)
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
    ok, reason, _errors = validate_citations(parsed, SOURCE)
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
    ok, reason, errors = validate_citations(parsed, SOURCE)
    assert ok, reason
    assert errors == []


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
    ok, reason, _errors = validate_citations(parsed, SOURCE)
    assert not ok
    assert "deadline" in reason


# ---------- Citation error details ----------


def test_validate_citations_returns_error_details() -> None:
    """When offsets are wrong, errors list contains the field, bad quote, and what
    text was actually at those offsets."""
    # Use offsets that point at "settlement" region, not "Acme Inc."
    bad_start, bad_end = 100, 109
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
            Citation(field="defendants", quote="Acme Inc.", start_offset=bad_start, end_offset=bad_end),
        ],
    )
    ok, _reason, errors = validate_citations(parsed, SOURCE)
    assert not ok
    assert len(errors) == 1
    err = errors[0]
    assert isinstance(err, CitationError)
    assert err.field == "defendants"
    assert err.quote == "Acme Inc."
    assert err.actual == SOURCE[bad_start:bad_end]


def test_missing_citation_error_has_empty_quote() -> None:
    """Missing citations produce an error with empty quote/actual."""
    parsed = _parsed(
        class_period_start=None,
        class_period_end=None,
        est_payout_low=None,
        est_payout_high=None,
        deadline=None,
        citations=[
            _cite("title", "Acme Inc. has agreed to a class action settlement"),
            # No citation for defendants
        ],
    )
    ok, _reason, errors = validate_citations(parsed, SOURCE)
    assert not ok
    assert any(e.field == "defendants" and e.quote == "" for e in errors)


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


# ---------- Multi-feed dedup ----------


def test_multi_feed_deduplicates() -> None:
    """When two feeds return the same URL, run_once should only process it once."""
    entry_a = FeedEntry(url="https://example.com/same", title="Class action A", summary="")
    entry_b = FeedEntry(url="https://example.com/same", title="Class action B", summary="")
    entry_c = FeedEntry(url="https://example.com/other", title="Class action C", summary="")

    with patch("agents.ingest.fetch_feed") as mock_fetch:
        mock_fetch.side_effect = [[entry_a, entry_c], [entry_b]]

        # We need to also mock the DB + Claude calls since run_once calls them.
        # Just verify the dedup logic by checking fetch_article_text call count.
        with (
            patch("agents.ingest._already_ingested", return_value=set()),
            patch("agents.ingest.fetch_article_text", return_value="") as mock_article,
            patch("agents.ingest._persist_rejected"),
        ):
            from agents.ingest import run_once

            run_once("https://feed1.com,https://feed2.com")
            # 2 unique URLs, not 3
            assert mock_article.call_count == 2


# ---------- End-to-end run_once ----------


def test_viable_case_is_parsed_and_saved() -> None:
    """A viable case goes straight to ICP generation and persistence."""
    good_parsed = _parsed(citations=[])

    with (
        patch("agents.ingest.fetch_feed", return_value=[
            FeedEntry(url="https://example.com/case", title="Class action", summary=""),
        ]),
        patch("agents.ingest._already_ingested", return_value=set()),
        patch("agents.ingest.fetch_article_text", return_value=SOURCE),
        patch("agents.ingest.upload_raw", return_value="raw/test.txt"),
        patch("agents.ingest.parse_case", return_value=good_parsed) as mock_parse,
        patch("agents.ingest.build_icp", return_value=MagicMock()) as mock_icp,
        patch("agents.ingest._persist_case_and_icp", return_value="test-id"),
    ):
        from agents.ingest import run_once

        result = run_once("https://feed.com")
        assert result.parsed == 1
        assert result.rejected == 0
        assert mock_parse.call_count == 1
        mock_icp.assert_called_once()


def test_not_viable_case_is_rejected() -> None:
    """A non-viable case is rejected without ICP generation."""
    rejected_parsed = _parsed(is_viable_class_action=False, reject_reason="not a class action")

    with (
        patch("agents.ingest.fetch_feed", return_value=[
            FeedEntry(url="https://example.com/case", title="Class action", summary=""),
        ]),
        patch("agents.ingest._already_ingested", return_value=set()),
        patch("agents.ingest.fetch_article_text", return_value=SOURCE),
        patch("agents.ingest.upload_raw", return_value="raw/test.txt"),
        patch("agents.ingest.parse_case", return_value=rejected_parsed),
        patch("agents.ingest._persist_rejected") as mock_reject,
    ):
        from agents.ingest import run_once

        result = run_once("https://feed.com")
        assert result.rejected == 1
        assert result.parsed == 0
        mock_reject.assert_called_once()
