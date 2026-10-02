.DEFAULT_GOAL := help
.PHONY: help install up down migrate test test-unit lint format generate

SYNTH_DIR := tests/fixtures/synthetic
SYNTH_SEED := 20261002
SYNTH_COUNT := 20

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python 3.12 and dependencies with uv
	uv sync

up: install ## Start Postgres and MinIO, wait until healthy, apply migrations
	docker compose up -d --wait
	$(MAKE) migrate

down: ## Stop services (keeps volumes)
	docker compose down

migrate: ## Apply database migrations
	uv run alembic upgrade head

test: ## Run all tests (integration tests need `make up`)
	uv run pytest

test-unit: ## Run fast tests that need no services or models
	uv run pytest -m "not integration and not docling" --no-cov

lint: ## Lint, format check and type check
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format: ## Auto-format and fix lint
	uv run ruff format .
	uv run ruff check --fix .

generate: ## Regenerate the synthetic invoice/PO pairs with ground truth
	uv run python -m docforge.synth --count $(SYNTH_COUNT) --seed $(SYNTH_SEED) --out $(SYNTH_DIR)
