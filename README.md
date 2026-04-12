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

## Local-dev loop (no AWS, no Meta, no Slack, no Ideogram, no Arcads)

The only paid service you need is **Anthropic** — all the others have stubs.

```sh
cp .env.local.example .env    # sets STORAGE_BACKEND=local + LOCAL_STUB_* flags
# then edit .env and paste your ANTHROPIC_API_KEY

make install                  # uv sync
make db-up                    # docker postgres
make migrate                  # create tables
make test                     # 46 unit tests

# Exercise one prompt without touching the DB:
make parse URL=https://www.prnewswire.com/news-releases/some-class-action.html
make parse-with-icp URL=...   # also runs the ICP generator

# Full pipeline locally, end-to-end:
make demo                     # seed synthetic case → creative → campaign → fake lead
# Look in .local-storage/ for generated PNG; .local-approvals/ for the Slack message;
# psql into localhost:5432 to see Case/Creative/Campaign/Lead rows.
```

### Stub modes

Every expensive client checks for its key, falling back to a stub when the key is missing (or when `LOCAL_STUB_<NAME>=1` is set).

| Service | Env flag | Stub behavior |
|---|---|---|
| S3 | `STORAGE_BACKEND=local` | Reads/writes under `.local-storage/<bucket>/` |
| Ideogram | `LOCAL_STUB_IMAGE=1` or no `IDEOGRAM_API_KEY` | Returns a 1×1 placeholder PNG |
| Arcads | `LOCAL_STUB_VIDEO=1` or no `ARCADS_API_KEY` | `submit_video` returns `stub-job-…`; `get_video` returns `completed` |
| Slack | `LOCAL_STUB_SLACK=1/reject/manual` or no `SLACK_BOT_TOKEN` | Writes `.local-approvals/<ts>.json`. `1`/unset auto-approves, `reject` auto-rejects, `manual` waits for a `<ts>.reaction` sidecar (created by `scripts/approve.py`) |
| Meta | `LOCAL_STUB_META=1` or no `META_ACCESS_TOKEN` | `deploy_lead_campaign` returns `local-camp-…`; `fetch_lead` reads `.local-leads/<id>.json` (or returns a default lead) |

Turn off any one stub to hit that specific real API — keep the rest stubbed.

### Manual approval workflow

```sh
# Boot with LOCAL_STUB_SLACK=manual in .env
make creative                 # posts the creative to .local-approvals/<ts>.json
make approve                  # list pending; shows the headline/body/image path
scripts/approve.py --approve <ts>   # (or --approve all, or --reject <ts>)
make creative                 # next tick — flips status to approved
make campaign                 # deploys (stub Meta, real deploy logs to console)
```

### Dry-run scripts

| Script | Purpose |
|---|---|
| `scripts/try_parse.py <url> [--with-icp]` | Run one press release through Claude, print parsed case + citation validation |
| `scripts/seed_case.py` | Insert a synthetic Case + ICP into local Postgres |
| `scripts/simulate_lead.py` | Sign a fake Meta webhook and invoke the handler directly |
| `scripts/approve.py` | List/approve/reject local creative approvals |

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
