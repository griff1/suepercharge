# Suepercharge

Autonomous legal lead-gen MVP. Three agents (ingest, creative, campaign) detect class-action press releases on PR Newswire, generate Meta ad creative, deploy campaigns, and capture leads.

See `CLAUDE.md` for architecture, commands, and compliance rails. The approved plan lives at `~/.claude/plans/peaceful-weaving-sundae.md`.

## Quickstart

```sh
uv sync
cp .env.example .env          # fill in the keys you have
uv run pytest -q              # unit tests (no network)
uv run ruff check .           # lint
```

## What's built

- 5-table Postgres schema with pgcrypto-ready PII columns
- `compliance.py` with UPL/FTC blocklist + versioned TCPA consent text
- Three Lambda agents that coordinate via row-status in Postgres (no orchestrator)
- Meta Marketing API deployer (not Manus AI; per plan §2)
- Slack-reaction approval gate (no Slack Events webhook)
- Terraform for 4 AWS services (Lambda, RDS, S3, EventBridge); `terraform validate` passes
- 34 passing unit tests

## Launch runbook

### 1. Parallel tracks (start immediately, multi-week lead time)

- **Retain counsel** on state bar lead-gen rules + FTC/Meta ad policy. Required sign-off on:
  - `compliance._UPL_PATTERNS` (the blocklist)
  - `compliance._CONSENT_VERSIONS["v1-2026-04"]` (the TCPA consent text)
  - The first 3 generated creatives
- **Meta Business Manager verification** (2–4 weeks). Create a Business Manager, add a Page, add an Ad Account, submit advertiser identity verification. Legal vertical ads get extra scrutiny.
- **Stripe conversation** (deferred — no billing in MVP, but early heads-up on the legal vertical avoids a surprise account freeze later).

### 2. Get API keys

- Anthropic (console.anthropic.com)
- Ideogram (ideogram.ai — API tier)
- Arcads (arcads.ai — API access)
- Meta: App ID / App Secret / Access Token / Ad Account ID / Page ID / Webhook Verify Token (generate a random string for the last one)
- Slack: Bot User OAuth Token + Channel ID + Signing Secret. Bot needs `chat:write`, `reactions:read`, `reactions:write`.

Load them into `.env` (for local runs) and into `dev.tfvars` (for Terraform; **never commit**).

### 3. Provision AWS

```sh
scripts/build_lambda.sh                    # needs Docker (arm64 cross-build)
cd infra/terraform
terraform init
terraform plan -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

Capture the `meta_webhook_url` output — you'll register it with Meta next.

### 4. Run DB migrations

The RDS instance is in the default VPC with no public access. From a bastion or `aws ssm start-session` tunnel:

```sh
DATABASE_URL="postgresql+psycopg://postgres:<pw>@<rds-endpoint>:5432/suepercharge" \
  uv run alembic upgrade head
```

### 5. Configure Meta

1. In the Meta App dashboard, subscribe the Page to `leadgen` webhook events.
2. Set callback URL = `meta_webhook_url` output; verify token = `META_WEBHOOK_VERIFY_TOKEN`.
3. Meta sends a GET; our webhook handler echoes the challenge — subscription confirms.
4. Publish the host Page (the legal name in the Meta profile must be the entity that signed the Meta advertising agreement).

### 6. Pre-flight verification (plan §7)

Before flipping any campaign to `ACTIVE`:

- [ ] `uv run pytest -q` — all green
- [ ] Seed one real press release URL, invoke the ingest Lambda manually, verify Case + ICP rows look right
- [ ] Let creative agent run, approve via Slack 👍, verify creative deploys in **PAUSED** state in Meta sandbox
- [ ] Meta ad review passes
- [ ] Submit a synthetic lead through the sandbox lead form — confirm `Lead` row lands with `consent_text_version` populated
- [ ] Lawyer sign-off on 3 generated creatives, the lead-form consent text, and the privacy policy page
- [ ] Flip the campaign to `ACTIVE` via `clients.meta_client.set_campaign_status(..., "ACTIVE")`

### 7. Go live

1 case, 1 state, $50/day daily budget, human approval gate on every creative. Scale state-by-state as legal review completes.

## Project layout

```
agents/            ingest.py, creative.py, campaign.py
clients/           anthropic_client, meta_client, ideogram_client, arcads_client, slack_client
prompts/           parse_case.j2, build_icp.j2, write_copy.j2, image_prompt.j2
models.py          5 ORM tables + Pydantic LLM schemas
compliance.py      blocklist + consent registry
db.py              SQLAlchemy session factory
migrations/        Alembic migrations (0001_initial)
infra/terraform/   main.tf + variables.tf
scripts/           build_lambda.sh
tests/             34 unit tests (no network)
```

## What's deferred

- Law firm CRM, enrichment, outbound email/calls, Stripe billing
- Admin UI (Next.js) — Slack + `psql` suffices until lead volume demands it
- Additional sources beyond PR Newswire (CourtListener RECAP, classaction.org, Bloomberg)
- Vector dedupe, Step Functions, Secrets Manager, multi-AZ RDS

See plan §8.
