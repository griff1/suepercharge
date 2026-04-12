"""Multi-variant copy generation — one Claude call produces N variants with
distinct angles. We validate structure (right count, right angles, in order)
and enforce the compliance blocklist across ALL variants."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from agents import creative as creative_mod
from models import GeneratedCopy, GeneratedCopyVariants


@dataclass
class _FakeCase:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    title: str = "Acme Class Action"
    defendants: list[str] = field(default_factory=lambda: ["Acme Inc."])
    product: str | None = "Acme Online Account"
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


def _variant(angle: str, primary: str) -> GeneratedCopy:
    return GeneratedCopy(
        angle=angle,
        headline=f"[{angle}] headline",
        primary_text=primary,
        cta="LEARN_MORE",
        rationale=f"{angle} rationale",
    )


def _all_clean() -> GeneratedCopyVariants:
    return GeneratedCopyVariants(
        variants=[
            _variant("informative", "A class action may provide compensation."),
            _variant("empathetic", "You may be eligible for compensation."),
            _variant("urgent", "Submit your info before the deadline to check eligibility."),
        ]
    )


def test_variants_happy_path_returns_all_three_in_order() -> None:
    with patch.object(creative_mod, "structured", return_value=_all_clean()) as m:
        result = creative_mod.generate_compliant_variants(
            case=_FakeCase(), icp=_FakeICP()
        )
    assert result is not None
    assert result.attempts == 1
    assert [v.angle for v in result.variants] == ["informative", "empathetic", "urgent"]
    assert m.call_count == 1


def test_variants_wrong_count_triggers_retry_then_gives_up() -> None:
    too_few = GeneratedCopyVariants(
        variants=[_variant("informative", "A class action may provide compensation.")]
    )
    with patch.object(creative_mod, "structured", side_effect=[too_few] * 10) as m:
        result = creative_mod.generate_compliant_variants(
            case=_FakeCase(), icp=_FakeICP()
        )
    assert result is None
    assert m.call_count == creative_mod.MAX_COPY_RETRIES


def test_variants_wrong_angle_order_rejected() -> None:
    scrambled = GeneratedCopyVariants(
        variants=[
            _variant("urgent", "Submit your info before the deadline."),
            _variant("informative", "A class action may provide compensation."),
            _variant("empathetic", "You may be eligible for compensation."),
        ]
    )
    clean_correct = _all_clean()
    with patch.object(creative_mod, "structured", side_effect=[scrambled, clean_correct]) as m:
        result = creative_mod.generate_compliant_variants(
            case=_FakeCase(), icp=_FakeICP()
        )
    assert result is not None
    assert result.attempts == 2
    assert m.call_count == 2


def test_variants_any_blocked_phrase_triggers_retry() -> None:
    """If ANY variant trips the blocklist, the whole response must regenerate."""
    bad_third = GeneratedCopyVariants(
        variants=[
            _variant("informative", "A class action may provide compensation."),
            _variant("empathetic", "You may be eligible for compensation."),
            _variant("urgent", "You have a case. Submit now."),  # UPL violation
        ]
    )
    good = _all_clean()
    with patch.object(creative_mod, "structured", side_effect=[bad_third, good]) as m:
        result = creative_mod.generate_compliant_variants(
            case=_FakeCase(), icp=_FakeICP()
        )
    assert result is not None
    assert result.attempts == 2
    assert m.call_count == 2
    # The clean set is what ultimately gets returned.
    assert all(v.angle in {"informative", "empathetic", "urgent"} for v in result.variants)


def test_ad_angles_constant_shape() -> None:
    """Changing the angle set is a deliberate scope change — lock it in."""
    assert creative_mod.ANGLE_NAMES == ("informative", "empathetic", "urgent")
    for a in creative_mod.AD_ANGLES:
        assert "name" in a
        assert "description" in a
