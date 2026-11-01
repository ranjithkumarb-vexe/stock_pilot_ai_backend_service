# StockPilot AI backend. `make help` lists targets.
SHELL := /bin/bash
UV ?= uv
PORT ?= 8000
export SP_TEST_DB ?= stockpilot_test

.PHONY: help setup test lint fmt run seed openapi migrate audit clean ingest-xbrl ingest-filings eval

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

.env:
	cp .env.example .env
	@echo "created .env from .env.example - edit DATABASE_URL / DATABASE_ADMIN_URL for your machine"

setup: .env ## uv sync, create dev+test databases, role stockpilot_app, run migrations
	$(UV) sync
	$(UV) run python -m scripts.setup_db

migrate: ## apply pending migrations to the dev database
	$(UV) run python -m app.core.migrate

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
	$(UV) run uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port $(PORT) --no-access-log

seed: ## universe + personas + demo data (BE-04)
	$(UV) run python -m scripts.seed

ingest-xbrl: ## SEC companyfacts -> xbrl_facts for the universe (live network; BE-11). TICKERS=A,B optional
	$(UV) run python -m app.modules.edgar.cli ingest-xbrl $(if $(TICKERS),--tickers $(TICKERS),)

ingest-filings: ## latest 10-K + 8-K list -> raw_documents/doc_chunks (live network; BE-12). TICKERS=A,B optional
	$(UV) run python -m app.modules.research.cli ingest-filings $(if $(TICKERS),--tickers $(TICKERS),)

eval: ## golden-set / safety / persona-simulation evals -> evals/results/latest.json (BE-20). FAST=1 skips the pytest-backed checks
	$(UV) run python -m evals.run $(if $(FAST),--fast,)

openapi: ## regenerate docs/contracts/openapi.json
	$(UV) run python -m scripts.dump_openapi

audit: ## dependency vulnerability scan
	$(UV) run pip-audit

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
