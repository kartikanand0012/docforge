.DEFAULT_GOAL := help
.PHONY: help install up down migrate api worker test test-unit lint format generate generate-scans eval eval-record

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

api: ## Run the API on http://127.0.0.1:8000 (needs GEMINI_API_KEY; no auth yet, local only)
	uv run uvicorn docforge.api.main:create_default_app --factory --host 127.0.0.1 --port 8000

worker: ## Run a worker that processes uploaded documents (needs GEMINI_API_KEY)
	uv run python -m docforge.worker

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
	uv run python -m docforge.synth --seeded --out tests/fixtures/seeded

generate-scans: ## Rewrite the scanned variants (their recorded parses must then be re-recorded)
	uv run python -m docforge.synth --scans-from $(SYNTH_DIR) --out tests/fixtures/scanned

eval: ## Re-run the invoice eval offline from recordings and rewrite the baseline report
	uv run python -m docforge.evals --mode replay
	uv run python -m docforge.evals --suite trust --mode replay
	uv run python -m docforge.evals --suite scans --mode replay

eval-record: ## Run the eval live for anything not yet recorded (needs GEMINI_API_KEY; resumable)
	uv run python -m docforge.evals --mode record
	uv run python -m docforge.evals --suite trust --mode record
	uv run python -m docforge.evals --suite scans --mode record
