"""Compliance blocklist must catch the obvious UPL/FTC violations.

These are the exact tripwires a state bar or FTC complaint would hit, so they
are non-negotiable. If a test here fails, the blocklist is too loose.
"""
from __future__ import annotations

import pytest

from compliance import (
    CURRENT_CONSENT_VERSION,
    blocklist_for_prompt,
    consent_text,
    copy_is_clean,
    scan_copy,
)

BAD_COPY = [
    "You have a case against Acme. Call now!",
    "Guaranteed recovery of up to $10,000.",
    "You will win your settlement.",
    "I used this drug and suffered harm — you could too.",
    "Official court notice: claim your money today.",
    "You are entitled to $5,000.",
    "Get $2,500 guaranteed today.",
    "It didn't work for me either.",
]

GOOD_COPY = [
    "You may be eligible for compensation if you purchased Acme between 2020 and 2023.",
    "A class action settlement may provide compensation to affected customers.",
    "Attorneys are reviewing claims. Submit your info to see if you qualify.",
]


@pytest.mark.parametrize("text", BAD_COPY)
def test_blocklist_catches_upl_phrases(text: str) -> None:
    findings = scan_copy(text)
    assert findings, f"Blocklist missed: {text!r}"
    assert not copy_is_clean(text)


@pytest.mark.parametrize("text", GOOD_COPY)
def test_blocklist_passes_clean_copy(text: str) -> None:
    findings = scan_copy(text)
    assert not findings, f"False positive on clean copy: {text!r} -> {findings}"
    assert copy_is_clean(text)


def test_blocklist_in_prompt_mentions_every_pattern() -> None:
    prompt = blocklist_for_prompt()
    # The literal pattern source is included so the LLM sees exactly what we check.
    assert "you have a case" in prompt
    assert "Safe alternatives" in prompt


def test_consent_text_versioned() -> None:
    text = consent_text(CURRENT_CONSENT_VERSION)
    assert "attorney advertising" in text.lower()
    assert "stop" in text.lower()  # opt-out language
    with pytest.raises(KeyError):
        consent_text("v99-never")
