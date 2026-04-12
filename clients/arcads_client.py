"""Arcads.ai video generation.

Async model: submit a job, receive a job_id, poll for completion. We store
the job_id on the Creative row and poll on the next creative-agent cron tick
rather than blocking inside a single Lambda invocation.

The exact endpoint/field names are an educated guess; update when the API
key arrives and we see real responses. Everything Arcads-specific is
contained here so the agent doesn't care about their wire format.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import httpx

JobStatus = Literal["queued", "processing", "completed", "failed"]

_BASE_URL = os.environ.get("ARCADS_API_BASE", "https://api.arcads.ai/v1")

# Default avatar to use for MVP. Override per-case later if we A/B avatars.
_DEFAULT_AVATAR_ID = os.environ.get("ARCADS_DEFAULT_AVATAR_ID", "")


@dataclass
class VideoJob:
    id: str
    status: JobStatus
    video_url: str | None
    error: str | None


def _client() -> httpx.Client:
    key = os.environ["ARCADS_API_KEY"]
    return httpx.Client(
        base_url=_BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        timeout=60.0,
    )


def submit_video(
    *,
    script: str,
    avatar_id: str | None = None,
    watermark_text: str | None = None,
    aspect_ratio: Literal["9x16", "1x1", "16x9"] = "9x16",
) -> str:
    """Submit a video generation job. Returns the job id."""
    payload = {
        "script": script,
        "avatar_id": avatar_id or _DEFAULT_AVATAR_ID,
        "aspect_ratio": aspect_ratio,
    }
    if watermark_text:
        # If Arcads supports burned-in overlays we use it; otherwise we'd
        # post-process with ffmpeg. Key name is a best guess.
        payload["watermark_text"] = watermark_text

    with _client() as c:
        resp = c.post("/videos", json=payload)
        resp.raise_for_status()
        return resp.json()["id"]


def get_video(job_id: str) -> VideoJob:
    with _client() as c:
        resp = c.get(f"/videos/{job_id}")
        resp.raise_for_status()
        data = resp.json()
    return VideoJob(
        id=data["id"],
        status=data["status"],
        video_url=data.get("video_url"),
        error=data.get("error"),
    )


def download_video(url: str) -> bytes:
    with httpx.Client(timeout=120.0) as c:
        resp = c.get(url)
        resp.raise_for_status()
        return resp.content
