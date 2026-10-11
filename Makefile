.DEFAULT_GOAL := help
.PHONY: help install up down migrate api worker test test-unit lint format generate generate-scans models web web-check e2e eval eval-record gate ops-check verify audit robustness

SYNTH_DIR := tests/fixtures/synthetic
SYNTH_SEED := 20261002
SYNTH_COUNT := 20

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install Python 3.12 and dependencies with uv
	uv sync

up: install ## Start Postgres and MinIO, wait until healthy, apply migrations
	docker compose up -d --wait
	$(MAKE) migrate

down: ## Stop services (keeps volumes)
	docker compose down

migrate: ## Apply database migrations (as the owner), then make the application's restricted login
	uv run alembic upgrade head
	uv run python -m docforge.db.roles

api: ## Run the API on http://127.0.0.1:8000 (needs GEMINI_API_KEY)
	uv run uvicorn docforge.api.main:create_default_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers

worker: ## Run a worker that processes uploaded documents (needs GEMINI_API_KEY)
	uv run python -m docforge.worker

test: ## Run all tests except the system tests (integration tests need `make up`)
	uv run pytest -m "not system"

test-unit: ## Run fast tests that need no services or models
	uv run pytest -m "not integration and not docling and not system" --no-cov

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
	uv run python -m docforge.synth --multipage --seed $(SYNTH_SEED) --out tests/fixtures/multipage
	uv run python -m docforge.synth --coa --seed $(SYNTH_SEED) --out tests/fixtures/coa

generate-scans: ## Rewrite the scanned variants (their recorded parses must then be re-recorded)
	uv run python -m docforge.synth --scans-from $(SYNTH_DIR) --out tests/fixtures/scanned

models: ## Download the parser's layout, table and OCR models once (then PARSER_OFFLINE=true works)
	uv run python -c "from docforge.parsing.docling_parser import DoclingParser; from pathlib import Path; [DoclingParser().parse(Path(p).read_bytes()) for p in ('tests/fixtures/synthetic/pair_001/invoice.pdf', 'tests/fixtures/scanned/scan_good/pair_001/invoice.pdf')]"

web: ## Run the review screen on http://localhost:3000 against the API on port 8000
	cd web && DOCFORGE_API_URL=$${DOCFORGE_API_URL:-http://127.0.0.1:8000} npm run dev

web-check: ## Type-check, lint, unit-test and build the review app in web/
	cd web && npm ci && npm run typecheck && npm run lint && npm test && npm run build

e2e: ## Browser test of the whole review flow on recorded documents (needs `make up`; no key)
	./scripts/e2e.sh

eval: ## Re-run the invoice eval offline from recordings and rewrite the baseline report
	uv run python -m docforge.evals --mode replay
	uv run python -m docforge.evals --mode replay --fixtures tests/fixtures/multipage --out evals/baselines/multipage.json
	uv run python -m docforge.evals --suite trust --mode replay
	uv run python -m docforge.evals --suite scans --mode replay
	uv run python -m docforge.evals --suite coa --mode replay
	uv run python -m docforge.evals --suite search --mode replay
	uv run python -m docforge.evals --suite search-heldout --mode replay
	uv run python -m docforge.evals --suite answers --mode replay
	uv run python -m docforge.evals --suite mcp --mode replay

gate: ## Check the eval reports against the floors in evals/gate.json (CI blocks on a failure)
	uv run python -m docforge.evals.gate

verify: ## Check the running demo stack (.deploytest, https://localhost) end to end over HTTPS
	uv run pytest -m "system and not robustness" --no-cov tests/system

robustness: ## Every hostile file through the real parser and converter on the demo stack, then back to replay
	@restore() { cd .deploytest && docker compose up -d --wait api worker; }; \
	trap restore EXIT INT TERM; \
	(cd .deploytest && docker compose -f compose.yml -f ../deploy/compose.capacity.yml up -d --wait api worker) && \
	uv run pytest -m robustness --no-cov -rA tests/system

audit: ## Look for known vulnerabilities in every locked dependency
	uv export -q --frozen --all-groups --no-emit-project --format requirements-txt -o .audit-requirements.txt
	uvx pip-audit@2.10.1 --disable-pip --progress-spinner off -r .audit-requirements.txt; status=$$?; rm -f .audit-requirements.txt; exit $$status

ops-check: ## What in the running system needs a person (exit 0 ok, 1 warning, 2 critical)
	uv run python -m docforge.ops check

eval-record: ## Run the eval live for anything not yet recorded (needs GEMINI_API_KEY; resumable)
	uv run python -m docforge.evals --mode record
	uv run python -m docforge.evals --mode record --fixtures tests/fixtures/multipage --out evals/baselines/multipage.json
	uv run python -m docforge.evals --suite trust --mode record
	uv run python -m docforge.evals --suite scans --mode record
	uv run python -m docforge.evals --suite coa --mode record
	uv run python -m docforge.evals --suite search --mode record
	uv run python -m docforge.evals --suite search-heldout --mode record
	uv run python -m docforge.evals --suite answers --mode record
