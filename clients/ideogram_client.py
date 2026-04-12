"""Ideogram 3.0 image generation.

Picked for legible in-image text (ad headlines read poorly on DALL-E/SD).
Minimal surface: one `generate(prompt, aspect_ratio)` call that returns the
generated image bytes. We upload to S3 from the caller.

API docs shape may evolve; the endpoint and payload keys are isolated here so
swapping to another image provider is a single-file change.
"""
from __future__ import annotations

import os
from typing import Literal

import httpx

AspectRatio = Literal["1x1", "4x5", "16x9", "9x16"]

_BASE_URL = os.environ.get("IDEOGRAM_API_BASE", "https://api.ideogram.ai")
_DEFAULT_MODEL = os.environ.get("IDEOGRAM_MODEL", "V_3")


def _client() -> httpx.Client:
    key = os.environ["IDEOGRAM_API_KEY"]
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"Api-Key": key},
        timeout=60.0,
    )


def generate(prompt: str, *, aspect_ratio: AspectRatio = "1x1", magic_prompt: bool = True) -> bytes:
    """Generate one image; return the PNG/JPEG bytes.

    We fetch the returned URL ourselves so the caller only handles bytes.
    """
    payload = {
        "image_request": {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "model": _DEFAULT_MODEL,
            "magic_prompt_option": "AUTO" if magic_prompt else "OFF",
        }
    }
    with _client() as c:
        resp = c.post("/generate", json=payload)
        resp.raise_for_status()
        data = resp.json()
        url = data["data"][0]["url"]
        img = c.get(url)
        img.raise_for_status()
        return img.content
