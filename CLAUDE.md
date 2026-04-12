# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Suepercharge — autonomous legal lead-gen MVP. Three agents (ingest, creative, campaign) detect class-action press releases on PR Newswire, generate Meta ad creative, deploy campaigns, and capture leads. Approved plan lives at `~/.claude/plans/peaceful-weaving-sundae.md`.

## Commands

- `uv sync` — install deps (Python 3.12, managed by uv)
- `uv run pytest -q` — run tests
- `uv run ruff check .` — lint
- `uv run ruff format .` — format
- `uv run alembic upgrade head` — apply DB migrations (requires `DATABASE_URL`)
- `uv run alembic revision --autogenerate -m "..."` — new migration
- `terraform -chdir=infra/terraform fmt` / `validate` / `plan` / `apply`
- `scripts/build_lambda.sh` — build the single Lambda zip used by all 4 functions (needs Docker for arm64 cross-build)

## Architecture (MVP, keep it small)

Four AWS services total: Lambda, EventBridge Scheduler, RDS Postgres (t4g.micro, single-AZ), S3. No DynamoDB, no Step Functions, no Secrets Manager, no API Gateway. Secrets live in Lambda env vars until revenue justifies otherwise.

The three agents coordinate via Postgres row status — no orchestrator, no queue:
- `agents/ingest.py` — cron 15m. Pulls PR Newswire RSS, parses cases with Claude (citation-enforced, fails closed), writes `Case` + `ICP`.
- `agents/creative.py` — cron 5m. Picks pending cases, generates copy (Claude) + image (Ideogram) + video (Arcads async, polled next tick), applies ffmpeg AI-disclosure watermark, posts to Slack for 👍/👎 approval.
- `agents/campaign.py` — cron 15m + Function URL webhook handler. Deploys approved creatives via Meta Marketing API (Advantage+ lead campaign), receives lead webhooks, writes `Lead` rows.

`models.py` has the 5-table schema + Pydantic LLM output targets. `compliance.py` has the UPL/FTC phrase blocklist, the versioned TCPA consent text registry, and the AI-disclosure string. `clients/anthropic_client.py` wraps Claude with enforced Pydantic structured output via tool-use.

## Non-negotiable compliance rails (enforced in code)

1. **No percentage-of-recovery billing.** Schema has no column for it. Flat fees only (state bar fee-splitting rules).
2. **UPL/FTC phrase blocklist** runs on every generated copy (`compliance.scan_copy`). Blocked phrases force LLM regeneration.
3. **Citation enforcement**: the ingest parser must return source char-offsets for every non-null Case field. Missing citations ⇒ reject the row.
4. **Human approval gate**: no Creative deploys without a Slack 👍 flipping `Creative.status` to `approved`.
5. **AI disclosure watermark**: every video gets the `compliance.AI_DISCLOSURE_TEXT` overlay; `ai_disclosure_applied=false` blocks deploy.
6. **TCPA consent text**: hardcoded on the Meta lead form; `Lead.consent_text_version` + `consent_ts` + `consent_ip` are mandatory.
7. **No consumer phone/SMS outreach.** MVP firms-only (and firms are out of scope entirely for now).

## Conventions

- Python 3.12, type-annotated, `from __future__ import annotations`.
- SQLAlchemy 2.0 mapped-style models. Alembic migrations are hand-written (schema is small; autogenerate drift is not worth it yet).
- Claude structured output via `clients.anthropic_client.structured(response_model=...)`. Don't call the SDK directly from agents.
- Every external HTTP call in tests uses `vcrpy` cassettes. Don't hit live APIs in tests.
