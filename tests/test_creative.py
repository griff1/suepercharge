"""Creative agent — the compliance-retry loop is the load-bearing check.

If Claude returns a blocked phrase, we must re-prompt with the reason and
only persist copy that passes `compliance.scan_copy`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from agents import creative as creative_mod
from models import GeneratedCopy


@dataclass
class _FakeCase:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    title: str = "Acme Class Action"
    defendants: list[str] = field(default_factory=lambda: ["Acme Inc."])
    product: str | None = None
    harm_type: str | None = "data breach"
    class_period_start: Any = None
    class_period_end: Any = None
    est_payout_low: int = 50
    est_payout_high: int = 500
    deadline: Any = None


@dataclass
class _FakeICP:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    demographics: dict[str, Any] = field(default_factory=lambda: {"age_range": "25-54"})
    psychographics: dict[str, Any] = field(default_factory=dict)
    targeting_hints: dict[str, Any] = field(default_factory=dict)


def _copy(primary_text: str, angle: str = "informative") -> GeneratedCopy:
    return GeneratedCopy(
        angle=angle,
        headline="Affected by a recent data breach?",
        primary_text=primary_text,
        cta="LEARN_MORE",
        rationale="Speaks to cost-conscious affected users.",
    )


def test_returns_compliant_copy_on_first_try() -> None:
    clean = _copy("A class action may provide compensation to affected customers.")
    with patch.object(creative_mod, "structured", return_value=clean) as m:
        result = creative_mod.generate_compliant_copy(case=_FakeCase(), icp=_FakeICP())
    assert result is not None
    assert result.attempts == 1
    assert m.call_count == 1


def test_retries_on_blocklist_violation_and_succeeds() -> None:
    bad = _copy("You have a case. Guaranteed recovery for you.")
    good = _copy("You may be eligible for compensation. Submit your info to check.")
    with patch.object(creative_mod, "structured", side_effect=[bad, good]) as m:
        result = creative_mod.generate_compliant_copy(case=_FakeCase(), icp=_FakeICP())
    assert result is not None
    assert result.attempts == 2
    assert m.call_count == 2


def test_gives_up_after_max_retries() -> None:
    # Every attempt is bad — we should stop at MAX_COPY_RETRIES and return None.
    bad = _copy("You have a case against them.")
    with patch.object(creative_mod, "structured", side_effect=[bad] * 10) as m:
        result = creative_mod.generate_compliant_copy(case=_FakeCase(), icp=_FakeICP())
    assert result is None
    assert m.call_count == creative_mod.MAX_COPY_RETRIES


@pytest.mark.parametrize("blocked_text", [
    "You have a case — call now.",
    "Guaranteed recovery of $5,000.",
    "I used this drug and suffered harm.",
])
def test_retry_reason_is_passed_back_into_prompt(blocked_text: str) -> None:
    """On retry, the second prompt must include the violation reason so Claude
    can avoid it. We assert by peeking at the prompt argument on call 2."""
    bad = _copy(blocked_text)
    good = _copy("You may be eligible for compensation if you purchased between 2020-2022.")
    prompts_seen: list[str] = []

    def _capture(**kwargs):
        prompts_seen.append(kwargs["prompt"])
        # Return bad on first call, good on second.
        return bad if len(prompts_seen) == 1 else good

    with patch.object(creative_mod, "structured", side_effect=_capture):
        result = creative_mod.generate_compliant_copy(case=_FakeCase(), icp=_FakeICP())

    assert result is not None
    assert len(prompts_seen) == 2
    assert "previous attempt was rejected" in prompts_seen[1].lower()
