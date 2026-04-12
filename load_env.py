"""Load `.env` at import time.

Scripts and local agent invocations import this so `ANTHROPIC_API_KEY` and
friends are available without the caller remembering to `export` them. In
Lambda there's no `.env` file, so `load_dotenv()` is a silent no-op and the
real env vars set by Terraform take precedence.

Import this module before any code that reads `os.environ.get(...)`.

`override=True` is deliberate: `.env` is the single source of truth during
local dev. Without it, stale shell env vars (e.g. an empty IDEOGRAM_API_KEY
left over from an earlier session) silently shadow the file. Pytest sets
the vars it needs explicitly via `monkeypatch` so this doesn't affect tests.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_here = Path(__file__).resolve().parent
for candidate in (_here / ".env", _here / ".env.local"):
    if candidate.exists():
        load_dotenv(candidate, override=True)

# Also try the conventional search-up-from-cwd path.
load_dotenv(override=True)

# Expose a no-op function so callers can `import load_env` purely for side
# effects without ruff marking the import unused.
def ensure() -> None:
    """Idempotent — just triggers this module's import-time side effects."""
    _ = os.environ
