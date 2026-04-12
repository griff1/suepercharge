#!/usr/bin/env -S uv run python
"""Ingest agent status dashboard.

Shows service health, recent activity, case breakdown, and the latest
parse/reject reasoning — everything you need to follow what the agent
is doing without reading raw logs.

Usage:
  scripts/status.py              # summary
  scripts/status.py --detail     # include latest case details + citation errors
  scripts/status.py --tail 30    # also show last N log lines
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Make the project root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, func, select

from models import ICP, Case, CaseStatus

LOG_PATH = Path(__file__).resolve().parent.parent / ".local-logs" / "ingest.log"


def _engine():
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://dillonjohnson@localhost:5432/suepercharge",
    )
    return create_engine(url)


def _service_status() -> dict:
    """Check launchd service status."""
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.suepercharge.ingest"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return {"running": False, "pid": None}
        for line in result.stdout.splitlines():
            if '"PID"' in line:
                pid = line.strip().rstrip(";").split("=")[-1].strip()
                return {"running": True, "pid": pid}
        return {"running": False, "pid": None}
    except Exception:
        return {"running": False, "pid": None}


def _ollama_status() -> str:
    """Check if Ollama is responding."""
    try:
        import httpx

        resp = httpx.get(
            os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434") + "/api/tags",
            timeout=5.0,
        )
        if resp.status_code == 200:
            models = [m["name"] for m in resp.json().get("models", [])]
            return f"ok ({', '.join(models)})"
        return f"error (status {resp.status_code})"
    except Exception as e:
        return f"unreachable ({e})"


def _db_summary(engine) -> dict:
    """Query case counts and recent activity."""
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(Case)).scalar()
        parsed = conn.execute(
            select(func.count()).select_from(Case).where(Case.status == CaseStatus.parsed)
        ).scalar()
        rejected = conn.execute(
            select(func.count()).select_from(Case).where(Case.status == CaseStatus.rejected)
        ).scalar()
        icps = conn.execute(select(func.count()).select_from(ICP)).scalar()

        # Recent activity (last hour)
        one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
        recent = conn.execute(
            select(func.count()).select_from(Case).where(Case.created_at >= one_hour_ago)
        ).scalar()

        # Latest case
        latest = conn.execute(
            select(Case.title, Case.status, Case.reject_reason, Case.created_at, Case.pr_newswire_url)
            .order_by(Case.created_at.desc())
            .limit(1)
        ).first()

    return {
        "total": total,
        "parsed": parsed,
        "rejected": rejected,
        "icps": icps,
        "recent_1h": recent,
        "latest": latest,
    }


def _recent_cases_with_icps(engine, limit: int = 10) -> list:
    """Get recent cases with their ICP details."""
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        cases = s.execute(
            select(Case).order_by(Case.created_at.desc()).limit(limit)
        ).scalars().all()

        results = []
        for case in cases:
            icp = None
            if case.icps:
                icp = case.icps[0]
            results.append((case, icp))
    return results


def _log_tail(n: int) -> str:
    """Read last N lines of the log file."""
    if not LOG_PATH.exists():
        return "(no log file found)"
    try:
        result = subprocess.run(
            ["tail", f"-{n}", str(LOG_PATH)],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout
    except Exception as e:
        return f"(error reading log: {e})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--detail", action="store_true", help="Show recent case details")
    ap.add_argument("--tail", type=int, default=0, help="Show last N log lines")
    args = ap.parse_args()

    # --- Service health ---
    svc = _service_status()
    ollama = _ollama_status()

    print("=" * 60)
    print("  SUEPERCHARGE INGEST AGENT STATUS")
    print("=" * 60)
    print()
    print(f"  Service:   {'RUNNING (PID ' + svc['pid'] + ')' if svc['running'] else 'STOPPED'}")
    print(f"  Ollama:    {ollama}")
    print(f"  Log file:  {LOG_PATH}")
    print()

    # --- Database summary ---
    try:
        engine = _engine()
        summary = _db_summary(engine)

        print("  --- Database ---")
        print(f"  Total cases:    {summary['total']}")
        print(f"    Parsed:       {summary['parsed']}")
        print(f"    Rejected:     {summary['rejected']}")
        print(f"  ICPs generated: {summary['icps']}")
        print(f"  Last hour:      {summary['recent_1h']} new cases")
        print()

        if summary["latest"]:
            row = summary["latest"]
            print("  --- Latest Case ---")
            print(f"  Title:   {row.title[:70]}")
            print(f"  Status:  {row.status}")
            if row.reject_reason:
                print(f"  Reason:  {row.reject_reason[:80]}")
            print(f"  URL:     {row.pr_newswire_url}")
            print(f"  Time:    {row.created_at}")
            print()

        # --- Detailed recent cases ---
        if args.detail:
            cases = _recent_cases_with_icps(engine)
            if cases:
                print("  --- Recent Cases (newest first) ---")
                print()
                for case, icp in cases:
                    status_icon = "+" if case.status.value == "parsed" else "x"
                    print(f"  [{status_icon}] {case.title[:65]}")
                    if case.status.value == "parsed":
                        defendants = ", ".join(case.defendants) if case.defendants else "(none)"
                        print(f"      Defendants: {defendants}")
                        if case.geography:
                            print(f"      Geography:  {case.geography}")
                        if case.deadline:
                            print(f"      Deadline:   {case.deadline}")
                        if case.harm_type:
                            print(f"      Harm:       {case.harm_type[:60]}")
                        if icp:
                            print("      --- ICP ---")
                            for label, data in [
                                ("WHO", icp.demographics),
                                ("INTERESTS", icp.psychographics),
                                ("AD ANGLE", icp.targeting_hints),
                                ("EXCLUDE", icp.disqualifiers),
                            ]:
                                if data:
                                    print(f"      {label}")
                                    for k, v in data.items():
                                        val = v if isinstance(v, str) else str(v)
                                        print(f"        {k:15s} {val[:60]}")
                    else:
                        print(f"      Reason: {case.reject_reason[:80] if case.reject_reason else '(none)'}")
                    print(f"      {case.created_at}")
                    print()

    except Exception as e:
        print(f"  Database: error ({e})")
        print()

    # --- Log tail ---
    if args.tail > 0:
        print("  --- Recent Logs ---")
        print()
        print(_log_tail(args.tail))

    print("=" * 60)
    print("  Commands: make agent-start | agent-stop | agent-restart")
    print("            make agent-logs   (live tail)")
    print("            scripts/status.py --detail --tail 50")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
