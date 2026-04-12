# Suepercharge — developer commands. Keep this file short.
# Philosophy: targets wrap the common incantations; if a target has more than
# 3 lines of logic, it belongs in scripts/ instead.

.PHONY: help
help:
	@awk 'BEGIN{FS=":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ----- setup -----

.PHONY: install
install: ## uv sync (install all deps)
	uv sync

# ----- local postgres -----

.PHONY: db-up
db-up: ## Start local Postgres (Docker)
	docker compose up -d postgres
	@until docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; do sleep 0.5; done
	@echo "postgres ready on localhost:5432"

.PHONY: db-down
db-down: ## Stop local Postgres (keeps volume)
	docker compose down

.PHONY: db-nuke
db-nuke: ## Stop local Postgres AND delete its volume
	docker compose down -v

.PHONY: db-shell
db-shell: ## psql into local Postgres
	docker compose exec postgres psql -U postgres suepercharge

# ----- alembic -----

.PHONY: migrate
migrate: ## Apply migrations to $(DATABASE_URL) or local pg
	DATABASE_URL=$${DATABASE_URL:-postgresql+psycopg://postgres:postgres@localhost:5432/suepercharge} \
		uv run alembic upgrade head

.PHONY: migration
migration: ## Create a new autogenerate migration — usage: make migration MSG="add foo"
	DATABASE_URL=$${DATABASE_URL:-postgresql+psycopg://postgres:postgres@localhost:5432/suepercharge} \
		uv run alembic revision --autogenerate -m "$(MSG)"

# ----- quality -----

.PHONY: test
test: ## Run unit tests
	uv run pytest -q

.PHONY: lint
lint: ## Ruff check
	uv run ruff check .

.PHONY: fmt
fmt: ## Ruff format
	uv run ruff format .

.PHONY: check
check: lint test ## Lint + test (CI-equivalent locally)

# ----- agent dry-runs (no DB needed) -----

.PHONY: parse
parse: ## Parse one URL (Claude only, no DB) — usage: make parse URL=https://...
	uv run scripts/try_parse.py "$(URL)"

.PHONY: parse-with-icp
parse-with-icp: ## Parse + generate ICP — usage: make parse-with-icp URL=https://...
	uv run scripts/try_parse.py "$(URL)" --with-icp

# ----- local agent runs (needs DB; stubs cover external APIs) -----

.PHONY: seed
seed: ## Insert a synthetic Case + ICP into local Postgres
	uv run scripts/seed_case.py

.PHONY: ingest
ingest: ## Run the ingest agent once against the live feed
	uv run python -m agents.ingest

.PHONY: creative
creative: ## Run the creative agent once (stubs kick in if keys missing)
	uv run python -m agents.creative

.PHONY: campaign
campaign: ## Run the campaign agent once (Meta stubbed if keys missing)
	uv run python -m agents.campaign

.PHONY: approve
approve: ## List pending local approvals; see `scripts/approve.py --help`
	uv run scripts/approve.py

.PHONY: simulate-lead
simulate-lead: ## Fire a fake Meta leadgen webhook at the handler
	uv run scripts/simulate_lead.py

# ----- one-shot local pipeline demo (no keys required beyond Anthropic) -----

.PHONY: demo
demo: ## Full local demo: seed -> creative -> campaign -> simulate a lead
	uv run scripts/seed_case.py
	uv run python -m agents.creative
	uv run python -m agents.campaign
	uv run scripts/simulate_lead.py

# ----- infra -----

.PHONY: tf-fmt
tf-fmt: ## Format terraform files
	terraform -chdir=infra/terraform fmt

.PHONY: tf-validate
tf-validate: ## Validate terraform config
	terraform -chdir=infra/terraform validate

.PHONY: lambda-zip
lambda-zip: ## Build the Lambda deployment zip (needs Docker)
	scripts/build_lambda.sh
