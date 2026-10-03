#!/usr/bin/env bash
# Load test: a fresh database, the real API (two processes) and worker on recorded parses and
# model replies (no key, no network), several organisations each uploading every recorded
# document at once, then searching and reading the review queue. Model time is not in these
# numbers: it is measured in the live eval and added in docs/progress.md.
# Needs `make up`. Usage: scripts/load.sh [organisations] [concurrency]
set -euo pipefail
set -m

cd "$(dirname "$0")/.."
TENANTS="${1:-5}"
CONCURRENCY="${2:-16}"
API_PORT="${LOAD_API_PORT:-8012}"
OUT="${LOAD_OUT:-evals/load/load.json}"
WORK="$(mktemp -d)"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  uv run python - <<'PY' >/dev/null 2>&1 || true
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from docforge.config import get_settings
url = make_url(get_settings().migration_database_url.get_secret_value())
with create_engine(url, isolation_level="AUTOCOMMIT").connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "docforge_load" WITH (FORCE)'))
PY
  rm -rf "$WORK"
}
trap cleanup EXIT

if lsof -nP -iTCP:"$API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $API_PORT is already in use; stop it first" >&2
  exit 1
fi

URLS="$(uv run python - <<'PY'
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from docforge.config import get_settings
settings = get_settings()
owner = make_url(settings.migration_database_url.get_secret_value())
app = make_url(settings.database_url.get_secret_value())
if owner.host not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("the load test only runs against a local Postgres")
with create_engine(owner, isolation_level="AUTOCOMMIT").connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "docforge_load" WITH (FORCE)'))
    conn.execute(text('CREATE DATABASE "docforge_load"'))
print(owner.set(database="docforge_load").render_as_string(hide_password=False))
print(app.set(database="docforge_load").render_as_string(hide_password=False))
PY
)"
export MIGRATION_DATABASE_URL="$(echo "$URLS" | sed -n 1p)"
export DATABASE_URL="$(echo "$URLS" | sed -n 2p)"
export PIPELINE_FACTORY="docforge.wiring:build_replay_pipelines"
export GEMINI_API_KEY=""
export SEARCHES_PER_MINUTE=100000
export MAX_PENDING_DOCUMENTS=100000

uv run alembic upgrade head >/dev/null
uv run python -m docforge.db.roles >/dev/null
for n in $(seq 1 "$TENANTS"); do
  uv run python -m docforge.admin create-tenant "load-$n" >/dev/null
  uv run python -m docforge.admin create-key --tenant "load-$n" --role admin --name load 2>/dev/null >>"$WORK/keys"
done
uv run python - "$WORK/questions.json" <<'PY'
import json, sys
from pathlib import Path
from docforge.evals.search import build_questions
questions = build_questions(Path("tests/fixtures/synthetic"), Path("tests/fixtures/coa"), 20)
Path(sys.argv[1]).write_text(json.dumps([q.text for q in questions[:20]]))
PY

uv run uvicorn docforge.api.main:create_default_app --factory --host 127.0.0.1 --port "$API_PORT" --workers 2 --no-proxy-headers --log-level warning >"$WORK/api.log" 2>&1 &
PIDS+=($!)
uv run python -m docforge.worker >"$WORK/worker.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null 2>&1 && break
  sleep 1
done

status=0
uv run python -m docforge.load --api "http://127.0.0.1:${API_PORT}" --keys "$WORK/keys" \
  --questions "$WORK/questions.json" --concurrency "$CONCURRENCY" --out "$OUT" || status=$?
if [ "$status" -ne 0 ]; then
  echo "--- api log ---"; tail -40 "$WORK/api.log"
  echo "--- worker log ---"; tail -40 "$WORK/worker.log"
fi
exit "$status"
