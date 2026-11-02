# StockPilot AI backend. `make help` lists targets.
SHELL := /bin/bash
UV ?= uv
PORT ?= 8000
export SP_TEST_DB ?= stockpilot_test
# AUTH_MODE has no default (SEC-03: dev auth mints tokens for any e-mail). Local targets choose dev explicitly; `make test`
# does not export it (the tests set it on their Settings), `make prod-check` sets its own.
DEVENV = AUTH_MODE=$${AUTH_MODE:-dev}

.PHONY: help setup test lint fmt run seed openapi migrate audit clean ingest-xbrl ingest-filings eval prod-check docker-build compose-up

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

.env:
	cp .env.example .env
	@echo "created .env from .env.example - edit DATABASE_URL / DATABASE_ADMIN_URL for your machine"

setup: .env ## uv sync, create dev+test databases, role stockpilot_app, run migrations
	$(UV) sync
	$(DEVENV) $(UV) run python -m scripts.setup_db

migrate: ## apply pending migrations to the dev database
	$(DEVENV) $(UV) run python -m app.core.migrate

test: ## full pytest suite with coverage (uses $(SP_TEST_DB))
	$(UV) run pytest --cov=app --cov-report=term-missing:skip-covered

lint: ## ruff + mypy --strict
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

fmt: ## auto-format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

run: ## uvicorn on :$(PORT) (override: make run PORT=8001)
	$(DEVENV) $(UV) run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port $(PORT) --no-access-log

seed: ## universe + personas + demo data (BE-04)
	$(DEVENV) $(UV) run python -m scripts.seed

ingest-xbrl: ## SEC companyfacts -> xbrl_facts for the universe (live network; BE-11). TICKERS=A,B optional
	$(DEVENV) $(UV) run python -m app.modules.edgar.cli ingest-xbrl $(if $(TICKERS),--tickers $(TICKERS),)

ingest-filings: ## latest 10-K + 8-K list -> raw_documents/doc_chunks (live network; BE-12). TICKERS=A,B optional
	$(DEVENV) $(UV) run python -m app.modules.research.cli ingest-filings $(if $(TICKERS),--tickers $(TICKERS),)

eval: ## golden-set / safety / persona-simulation evals -> evals/results/latest.json (BE-20). FAST=1 skips the pytest-backed checks and writes latest.fast.json (never latest.json)
	$(DEVENV) $(UV) run python -m evals.run $(if $(FAST),--fast,)

openapi: ## regenerate docs/contracts/openapi.json
	$(UV) run python -m scripts.dump_openapi

audit: ## dependency vulnerability scan
	$(UV) run pip-audit

prod-check: ## boot in production mode (APP_ENV=prod AUTH_MODE=supabase, fake secrets) on a scratch DB and probe it (no Docker needed)
	$(UV) run python -m scripts.prod_start_check

docker-build: ## build the production image (needs Docker)
	docker build -t stockpilot-api .

compose-up: ## demo stack: pgvector + migrate + api (needs Docker); see infra/docker-compose.yml
	docker compose -f infra/docker-compose.yml up --build

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
