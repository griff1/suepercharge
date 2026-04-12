"""Unified storage abstraction: S3 in prod, local filesystem in dev.

Selected by the `STORAGE_BACKEND` env var:
  - unset / "s3"  → boto3 against `S3_BUCKET`
  - "local"       → writes to ./.local-storage/<S3_BUCKET>/<key>

The API is intentionally narrow: put_bytes / put_text / get_bytes / presign.
Agents should go through here instead of touching boto3 directly so that
`make test` and `scripts/try_*.py` work without AWS credentials.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote


def _backend() -> str:
    return (os.environ.get("STORAGE_BACKEND") or "s3").lower()


def _bucket() -> str:
    # For the local backend this is just the top-level dir name.
    return os.environ.get("S3_BUCKET") or "suepercharge-local"


def _local_root() -> Path:
    root = Path(os.environ.get("LOCAL_STORAGE_ROOT", ".local-storage")) / _bucket()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _s3_client():
    import boto3

    return boto3.client("s3")


# ---------- public API ----------


def put_bytes(key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
    if _backend() == "local":
        path = _local_root() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return
    _s3_client().put_object(Bucket=_bucket(), Key=key, Body=body, ContentType=content_type)


def put_text(key: str, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
    put_bytes(key, body.encode("utf-8"), content_type=content_type)


def get_bytes(key: str) -> bytes:
    if _backend() == "local":
        return (_local_root() / key).read_bytes()
    return _s3_client().get_object(Bucket=_bucket(), Key=key)["Body"].read()


def presign(key: str, expires: int = 3600) -> str:
    """Return a URL that can be opened by a human reviewer.

    - S3: presigned HTTPS URL.
    - Local: a `file://` URL to the on-disk copy. Slack won't render these as
      inline images, but the Slack stub bypasses that path anyway.
    """
    if _backend() == "local":
        path = (_local_root() / key).absolute()
        return "file://" + quote(str(path))
    return _s3_client().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": key}, ExpiresIn=expires
    )


def describe() -> str:
    """Human-readable description of the active backend; handy for CLI output."""
    if _backend() == "local":
        return f"local:{_local_root()}"
    return f"s3://{_bucket()}"
