"""Ideogram 3.0 image generation.

Picked for legible in-image text (ad headlines read poorly on DALL-E/SD) and
strong ad-context composition. Minimal surface: one `generate(prompt, ...)`
call that returns the generated image bytes. The caller uploads to S3 /
local storage via `storage.put_bytes`.

Endpoint details (verified against Ideogram docs as of 2026-04):
  POST https://api.ideogram.ai/v1/ideogram-v3/generate
  Headers: Api-Key: <key>
  Body: multipart/form-data with flat fields (NOT JSON, NOT nested under
        "image_request" like the v1 API).
  Response: { "data": [ { "url": "...", ... } ] } — the url expires quickly,
            so we fetch it inside the same client session.
"""
from __future__ import annotations

import logging
import os
from typing import Literal

import httpx

AspectRatio = Literal["1x1", "4x5", "16x9", "9x16"]
RenderingSpeed = Literal["FLASH", "TURBO", "DEFAULT", "QUALITY"]
StyleType = Literal["AUTO", "GENERAL", "REALISTIC", "DESIGN", "FICTION"]

_BASE_URL = os.environ.get("IDEOGRAM_API_BASE", "https://api.ideogram.ai")
_DEFAULT_RENDERING_SPEED: RenderingSpeed = os.environ.get(
    "IDEOGRAM_RENDERING_SPEED", "DEFAULT"
)  # type: ignore[assignment]

# Default negative prompt to block typography — Meta's ad formatter adds the
# headline/body text itself, and Ideogram's auto-generated text is often
# misspelled, cropped, or stylistically inconsistent. Strong negative prompts
# plus style_type=REALISTIC keep the model in photography mode.
_DEFAULT_NEGATIVE_PROMPT = (
    "text, letters, words, typography, writing, captions, headlines, signs, "
    "billboards, logos, watermarks, subtitles, handwriting, numbers, labels"
)

log = logging.getLogger(__name__)


def _stub_enabled() -> bool:
    """Stub mode: LOCAL_STUB_IMAGE=1 OR IDEOGRAM_API_KEY unset."""
    if os.environ.get("LOCAL_STUB_IMAGE") in {"1", "true", "yes"}:
        return True
    return not os.environ.get("IDEOGRAM_API_KEY")


# Smallest valid 1x1 PNG (1 pixel, solid gray). Good enough for local DB rows.
_STUB_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c63f8cfc0000000030001015a2d0b670000000049454e44ae426082"
)


def _client() -> httpx.Client:
    key = os.environ["IDEOGRAM_API_KEY"]
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"Api-Key": key},
        timeout=120.0,
    )


def generate(
    prompt: str,
    *,
    aspect_ratio: AspectRatio = "1x1",
    magic_prompt: bool = False,
    rendering_speed: RenderingSpeed | None = None,
    style_type: StyleType = "REALISTIC",
    negative_prompt: str | None = None,
    suppress_text: bool = True,
) -> bytes:
    """Generate one image; return the PNG/JPEG bytes.

    Defaults chosen for ad-creative use:
      - style_type=REALISTIC: photography, not typography/design layouts.
      - magic_prompt=False:  Ideogram's auto-enhancement often adds text to
        prompts that mention "ads", "campaigns", or quoted phrases.
      - suppress_text=True:  prepends a strong negative prompt forbidding
        any written characters. Meta's ad formatter overlays the copy itself.

    In stub mode (no key or LOCAL_STUB_IMAGE=1), returns a tiny placeholder PNG.
    Otherwise hits Ideogram 3.0 and raises on any unexpected response shape
    or on an empty image download (fail-loud beats silent empty files).
    """
    if _stub_enabled():
        log.info("[stub:ideogram] returning placeholder PNG for prompt=%r", prompt[:60])
        return _STUB_PNG

    speed = rendering_speed or _DEFAULT_RENDERING_SPEED

    # Combine caller's negative prompt with the text-suppression default.
    neg_parts = [p for p in [negative_prompt, _DEFAULT_NEGATIVE_PROMPT if suppress_text else None] if p]
    negative = ", ".join(neg_parts) if neg_parts else None

    # Ideogram v3 requires multipart/form-data. We use `files=` with
    # (None, value) tuples to force multipart encoding without any binary
    # payload — httpx's `data=` alone would send application/x-www-form-urlencoded.
    files: list[tuple[str, tuple[None, str]]] = [
        ("prompt", (None, prompt)),
        ("aspect_ratio", (None, aspect_ratio)),
        ("rendering_speed", (None, speed)),
        ("magic_prompt", (None, "AUTO" if magic_prompt else "OFF")),
        ("style_type", (None, style_type)),
    ]
    if negative:
        files.append(("negative_prompt", (None, negative)))

    with _client() as c:
        log.info(
            "ideogram POST /v1/ideogram-v3/generate (speed=%s, ar=%s, style=%s, suppress_text=%s)",
            speed, aspect_ratio, style_type, suppress_text,
        )
        resp = c.post("/v1/ideogram-v3/generate", files=files)
        if resp.status_code >= 400:
            # Surface Ideogram's error body — they're good at explaining 4xx.
            raise RuntimeError(
                f"Ideogram generate failed: HTTP {resp.status_code} — {resp.text[:500]}"
            )
        body = resp.json()
        items = body.get("data") or []
        if not items:
            raise RuntimeError(f"Ideogram returned no images: {body}")
        url = items[0].get("url")
        if not url:
            raise RuntimeError(f"Ideogram returned no image URL: {items[0]}")
        log.info("ideogram generated: %s", url[:80])

        # The image URL is on a different host (CDN) so we use a fresh client
        # without the Api-Key header — some CDN providers reject unexpected auth.
    with httpx.Client(timeout=60.0) as dl:
        img = dl.get(url)
        if img.status_code >= 400:
            raise RuntimeError(
                f"Ideogram image download failed: HTTP {img.status_code} for {url[:80]}"
            )
        if not img.content:
            raise RuntimeError(f"Ideogram image download was empty (0 bytes) for {url[:80]}")
        log.info("ideogram downloaded %d bytes", len(img.content))
        return img.content
