"""Compliance guardrails enforced in code.

Everything here exists because a human rule has teeth:
- UPL (unauthorized practice of law) — phrases that imply we're promising a
  legal outcome or giving legal advice. State bars treat this harshly.
- FTC Endorsement Guides (16 CFR 255) — fake testimonials; AI avatars cannot
  claim personal experience with the product.
- TCPA — any consumer contact we enable requires captured prior express
  written consent at the moment of form fill.

This file is not a legal review substitute. The lawyer on the launch blocker
list must sign off on the blocklist and the consent text versions before we
go live.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------- UPL / FTC copy blocklist ----------

# Patterns are compiled case-insensitive, word-boundary aware where it matters.
# Prefer *specific* phrases over broad ones — false positives will force the
# LLM to rewrite forever.
_UPL_PATTERNS: tuple[str, ...] = (
    # Outcome guarantees / UPL
    r"\byou have a case\b",
    r"\byou (will|are going to) win\b",
    r"\bguaranteed\s+(recovery|settlement|payout|compensation)\b",
    r"\byou are entitled to\b",                         # use "may be entitled"
    r"\bwe will (win|get|recover) (you|your)\b",
    # Dollar promises
    r"\bget\s+\$[\d,]+\b",
    r"\breceive\s+(up to\s+)?\$[\d,]+\s+(guaranteed|today)\b",
    # Fake testimonial / first-person product experience (FTC 16 CFR 255)
    r"\bi (used|took|bought|tried) (this|the) (product|drug|device)\b",
    r"\bmy experience with\b",
    r"\bit (worked|didn'?t work) for me\b",
    # Misleading urgency
    r"\bclaim your money (now|today)\b",
    r"\bdon'?t miss out on your (check|settlement)\b",
    # Impersonating government/court
    r"\bofficial (court|government) notice\b",
    r"\bthe court has determined that you\b",
)

_COMPILED_UPL = tuple(re.compile(p, re.IGNORECASE) for p in _UPL_PATTERNS)


@dataclass(frozen=True)
class PolicyFinding:
    pattern: str
    match: str
    start: int
    end: int


def scan_copy(text: str) -> list[PolicyFinding]:
    """Return every UPL/FTC violation found in `text`. Empty list = clean."""
    findings: list[PolicyFinding] = []
    for compiled, source in zip(_COMPILED_UPL, _UPL_PATTERNS, strict=True):
        for m in compiled.finditer(text):
            findings.append(
                PolicyFinding(pattern=source, match=m.group(0), start=m.start(), end=m.end())
            )
    return findings


def copy_is_clean(text: str) -> bool:
    return not scan_copy(text)


# Human-readable version of the blocklist for inclusion in LLM prompts.
def blocklist_for_prompt() -> str:
    lines = [
        "NEVER use phrasing that matches any of these patterns (these are",
        "prohibited by state bar UPL rules and the FTC Endorsement Guides):",
    ]
    lines.extend(f"  - {p}" for p in _UPL_PATTERNS)
    lines.extend([
        "",
        "Safe alternatives:",
        "  - Instead of 'you have a case', say 'you may be eligible'",
        "  - Instead of dollar guarantees, say 'potential compensation available'",
        "  - Never write in the first person about experiencing the product",
    ])
    return "\n".join(lines)


# ---------- TCPA consent text registry ----------

# Versioned so we can prove, for every Lead row, the exact consent language the
# user saw. Retain each version for 4+ years (TCPA statute of limitations).
#
# Adding a new version: append, never edit. The Lead row references by key.

_CONSENT_VERSIONS: dict[str, str] = {
    "v1-2026-04": (
        "By submitting this form I agree that Suepercharge and its partner law "
        "firms may contact me by phone, text (including autodialed and "
        "prerecorded messages), and email about this and similar class action "
        "matters, using the contact information I provided, even if my number is "
        "on a do-not-call list. Consent is not a condition of any purchase. "
        "Message and data rates may apply. Reply STOP to opt out. This is "
        "attorney advertising; prior results do not guarantee a similar outcome."
    ),
}

CURRENT_CONSENT_VERSION = "v1-2026-04"


def consent_text(version: str = CURRENT_CONSENT_VERSION) -> str:
    if version not in _CONSENT_VERSIONS:
        raise KeyError(f"Unknown consent version: {version}")
    return _CONSENT_VERSIONS[version]


# ---------- AI-disclosure watermark ----------

# The string overlaid on every generated video. Disclosure laws (CA AB 2655,
# TX SB 751, TN ELVIS Act) and Meta's synthetic-media policy require this even
# when the avatar likeness is licensed (as Arcads is).
AI_DISCLOSURE_TEXT = "AI-generated • Suepercharge"
