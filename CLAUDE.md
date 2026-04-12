# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Suepercharge — autonomous legal lead-gen MVP. Three agents (ingest, creative, campaign) detect class-action press releases on PR Newswire, generate Meta ad creative, deploy campaigns, and capture leads. Approved plan lives at `~/.claude/plans/peaceful-weaving-sundae.md`.

## Commands

Prefer `make help` — the Makefile wraps every common incantation. Frequent ones:

- `make install` / `make test` / `make lint` / `make check`
- `make db-up` / `make migrate` / `make db-shell` / `make db-nuke`
- `make parse URL=...` — single-URL Claude dry-run (no DB, no AWS)
- `make seed` — insert a synthetic case into local Postgres
- `make ingest` / `make creative` / `make campaign` — run one tick of an agent locally
- `make approve` — list pending creative approvals in manual stub mode
- `make simulate-lead` — invoke the Meta webhook handler with a signed fake lead
- `make demo` — seed → creative → campaign → simulate lead, end-to-end with stubs
- `make tf-validate` / `make lambda-zip` — infra commands

## Local-dev: stub modes

Every paid external service has a stub that activates when its key is missing (or when `LOCAL_STUB_<NAME>=1` is forced). The only live dep required is Anthropic. See `.env.local.example` for the full set.

- Storage: `STORAGE_BACKEND=local` writes to `./.local-storage/` instead of S3.
- Slack stub writes approvals to `./.local-approvals/<ts>.json`; `LOCAL_STUB_SLACK=manual` waits for a `<ts>.reaction` sidecar file. `scripts/approve.py` lists/creates those.
- Arcads stub returns `completed` on first poll with a 1-byte MP4.
- Ideogram stub returns a 1×1 PNG.
- Meta stub returns `local-*` IDs for every object, and `fetch_lead` reads `./.local-leads/<id>.json` (or returns a default shape). `scripts/simulate_lead.py` drives this.

Agents coordinate via row status in Postgres, not a queue. When iterating locally: update code → run the specific agent's Make target → psql to inspect rows.

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
