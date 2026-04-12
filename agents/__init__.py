"""Agents package. Importing any agent from here also loads `.env` if present,
so `python -m agents.ingest` works without the caller pre-exporting env vars.
In Lambda there's no `.env` so this is a silent no-op."""
from __future__ import annotations

# load_env lives at repo root; add it to the path if we weren't launched from there.
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import load_env  # noqa: F401,E402 -- side effect: loads .env
