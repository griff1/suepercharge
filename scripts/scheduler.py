#!/usr/bin/env python
"""Local scheduler with credit guardrails.

Runs enrichment → outreach → eval on configurable intervals, with hard daily
caps on emails, calls, and LLM API calls to prevent runaway spend.

Usage:
    uv run scripts/scheduler.py          # uses defaults from env
    uv run scripts/scheduler.py --once   # single tick, then exit

Environment variables (all optional, sane defaults):
    SCHED_ENRICHMENT_INTERVAL_M   Minutes between enrichment ticks (default: 15)
    SCHED_OUTREACH_INTERVAL_M     Minutes between outreach ticks   (default: 15)
    SCHED_EVAL_INTERVAL_M         Minutes between eval ticks       (default: 360 = 6h)

    DAILY_EMAIL_CAP               Max emails per day    (default: 50)
    DAILY_CALL_CAP                Max calls per day     (default: 20)
    DAILY_LLM_CAP                 Max LLM calls per day (default: 200)
    MAX_TICKS                     Total ticks before auto-shutdown (default: 100, 0=unlimited)

The scheduler does NOT run forever by default — MAX_TICKS ensures it stops.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

log = logging.getLogger(__name__)
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

# --- Intervals (minutes) ---
ENRICHMENT_INTERVAL = int(os.environ.get("SCHED_ENRICHMENT_INTERVAL_M", "15")) * 60
OUTREACH_INTERVAL = int(os.environ.get("SCHED_OUTREACH_INTERVAL_M", "15")) * 60
EVAL_INTERVAL = int(os.environ.get("SCHED_EVAL_INTERVAL_M", "360")) * 60

# --- Daily caps ---
DAILY_EMAIL_CAP = int(os.environ.get("DAILY_EMAIL_CAP", "50"))
DAILY_CALL_CAP = int(os.environ.get("DAILY_CALL_CAP", "20"))
DAILY_LLM_CAP = int(os.environ.get("DAILY_LLM_CAP", "200"))
MAX_TICKS = int(os.environ.get("MAX_TICKS", "100"))


@dataclass
class DailyCounters:
    """Reset at midnight UTC. Tracks usage against caps."""
    date: str = ""
    emails: int = 0
    calls: int = 0
    llm_calls: int = 0

    def maybe_reset(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self.date != today:
            log.info("daily counters reset (was %s, now %s)", self.date or "init", today)
            self.date = today
            self.emails = 0
            self.calls = 0
            self.llm_calls = 0

    def can_email(self) -> bool:
        return self.emails < DAILY_EMAIL_CAP

    def can_call(self) -> bool:
        return self.calls < DAILY_CALL_CAP

    def can_llm(self) -> bool:
        return self.llm_calls < DAILY_LLM_CAP

    def summary(self) -> dict:
        return {
            "date": self.date,
            "emails": f"{self.emails}/{DAILY_EMAIL_CAP}",
            "calls": f"{self.calls}/{DAILY_CALL_CAP}",
            "llm_calls": f"{self.llm_calls}/{DAILY_LLM_CAP}",
        }


counters = DailyCounters()


def _run_enrichment() -> dict:
    counters.maybe_reset()
    if not counters.can_llm():
        log.warning("LLM daily cap reached — skipping enrichment")
        return {"skipped": "llm_cap"}

    from agents import enrichment
    result = enrichment.run_once()
    counters.llm_calls += result.attorneys_created + result.cases_skipped  # ~1 LLM call per case
    log.info("enrichment: %d attorneys created", result.attorneys_created)
    return {"attorneys_created": result.attorneys_created}


def _run_outreach() -> dict:
    counters.maybe_reset()
    if not counters.can_email() and not counters.can_call():
        log.warning("email + call daily caps reached — skipping outreach")
        return {"skipped": "all_caps"}

    # Temporarily patch caps into env so outreach respects limits
    os.environ["_SCHED_REMAINING_EMAILS"] = str(DAILY_EMAIL_CAP - counters.emails)
    os.environ["_SCHED_REMAINING_CALLS"] = str(DAILY_CALL_CAP - counters.calls)

    from agents import outreach
    result = outreach.run_once()

    counters.emails += result.new_emails + result.calendly_sent + result.follow_ups
    counters.calls += result.calls_placed
    counters.llm_calls += result.new_emails  # 1 LLM call per email generation

    log.info(
        "outreach: emails=%d calendly=%d calls=%d follow_ups=%d",
        result.new_emails, result.calendly_sent, result.calls_placed, result.follow_ups,
    )
    return {
        "new_emails": result.new_emails,
        "calendly_sent": result.calendly_sent,
        "calls_placed": result.calls_placed,
        "follow_ups": result.follow_ups,
    }


def _run_eval() -> dict:
    counters.maybe_reset()
    if not counters.can_llm():
        log.warning("LLM daily cap reached — skipping eval")
        return {"skipped": "llm_cap"}

    from agents import eval as eval_agent
    result = eval_agent.run_once()
    counters.llm_calls += result.experiments_evaluated  # 1 LLM call per experiment
    log.info("eval: %d evaluated, %d skipped", result.experiments_evaluated, result.experiments_skipped)
    return {
        "evaluated": result.experiments_evaluated,
        "skipped": result.experiments_skipped,
        "retired": result.variants_retired,
        "created": result.variants_created,
    }


def run_once() -> dict:
    """Single tick: enrichment → outreach → eval. Returns summary."""
    results = {}
    for name, fn in [("enrichment", _run_enrichment), ("outreach", _run_outreach), ("eval", _run_eval)]:
        try:
            results[name] = fn()
        except Exception:
            log.exception("%s failed", name)
            results[name] = {"error": True}
    results["counters"] = counters.summary()
    return results


def run_loop() -> None:
    """Run on intervals until MAX_TICKS or Ctrl-C."""
    last_enrichment = 0.0
    last_outreach = 0.0
    last_eval = 0.0
    total_ticks = 0

    log.info(
        "scheduler starting — intervals: enrichment=%dm outreach=%dm eval=%dm",
        ENRICHMENT_INTERVAL // 60, OUTREACH_INTERVAL // 60, EVAL_INTERVAL // 60,
    )
    log.info(
        "daily caps: emails=%d calls=%d llm=%d | max_ticks=%s",
        DAILY_EMAIL_CAP, DAILY_CALL_CAP, DAILY_LLM_CAP,
        MAX_TICKS if MAX_TICKS > 0 else "unlimited",
    )

    try:
        while True:
            if MAX_TICKS > 0 and total_ticks >= MAX_TICKS:
                log.info("MAX_TICKS (%d) reached — shutting down", MAX_TICKS)
                break

            now = time.monotonic()
            ran_something = False

            if now - last_enrichment >= ENRICHMENT_INTERVAL:
                log.info("--- enrichment tick ---")
                try:
                    _run_enrichment()
                except Exception:
                    log.exception("enrichment tick failed")
                last_enrichment = now
                ran_something = True

            if now - last_outreach >= OUTREACH_INTERVAL:
                log.info("--- outreach tick ---")
                try:
                    _run_outreach()
                except Exception:
                    log.exception("outreach tick failed")
                last_outreach = now
                ran_something = True

            if now - last_eval >= EVAL_INTERVAL:
                log.info("--- eval tick ---")
                try:
                    _run_eval()
                except Exception:
                    log.exception("eval tick failed")
                last_eval = now
                ran_something = True

            if ran_something:
                total_ticks += 1
                log.info("tick %d/%s complete | %s", total_ticks, MAX_TICKS or "∞", json.dumps(counters.summary()))

            # Sleep until the next earliest interval fires
            next_enrichment = last_enrichment + ENRICHMENT_INTERVAL - time.monotonic()
            next_outreach = last_outreach + OUTREACH_INTERVAL - time.monotonic()
            next_eval = last_eval + EVAL_INTERVAL - time.monotonic()
            sleep_s = max(1, min(next_enrichment, next_outreach, next_eval))
            time.sleep(sleep_s)

    except KeyboardInterrupt:
        log.info("scheduler stopped by user")

    log.info("final counters: %s", json.dumps(counters.summary()))


def main() -> None:
    if "--once" in sys.argv:
        result = run_once()
        print(json.dumps(result, indent=2))
    else:
        run_loop()


if __name__ == "__main__":
    main()
