# StockPilot AI backend. `make help` lists targets.
SHELL := /bin/bash
UV ?= uv
PORT ?= 8000
export SP_TEST_DB ?= stockpilot_test

.PHONY: help setup test lint fmt run seed openapi migrate audit clean

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

openapi: ## regenerate docs/contracts/openapi.json
	$(UV) run python -m scripts.dump_openapi

audit: ## dependency vulnerability scan
	$(UV) run pip-audit

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
