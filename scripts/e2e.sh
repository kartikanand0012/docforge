#!/usr/bin/env bash
# End-to-end: a fresh database, the real API and worker on recorded parses and model replies
# (no key, no network), the built review app, and Playwright driving a browser through it.
# Needs `make up` (Postgres and MinIO). Everything started here is stopped on exit.
set -euo pipefail
set -m  # each background job gets its own process group, so cleanup stops its children too

cd "$(dirname "$0")/.."
API_PORT="${E2E_API_PORT:-8011}"
WEB_PORT="${E2E_WEB_PORT:-3011}"
LOGS="$(mktemp -d)"
PIDS=()
CREATED=0

cleanup() {
  for pid in "${PIDS[@]}"; do kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  # Only what this run created, only on a local server, and from the maintenance database:
  # Postgres will not drop the database a connection is using.
  if [ "$CREATED" = 1 ]; then
    uv run python - <<'PY' >/dev/null 2>&1 || true
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from docforge.config import get_settings
url = make_url(get_settings().migration_database_url.get_secret_value())
if url.host in {"127.0.0.1", "localhost", "::1"}:
    with create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT").connect() as conn:
        conn.execute(text('DROP DATABASE IF EXISTS "docforge_e2e" WITH (FORCE)'))
PY
  fi
  rm -rf "${WORK:-}" "${LOGS:-}"
}
trap cleanup EXIT

for port in "$API_PORT" "$WEB_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $port is already in use (an earlier run still going?); stop it first" >&2
    exit 1
  fi
done

# Two lines out: the owner's URL (for migrations) and the application's (for the services).
URLS="$(uv run python - <<'PY'
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from docforge.config import get_settings
settings = get_settings()
owner = make_url(settings.migration_database_url.get_secret_value())
app = make_url(settings.database_url.get_secret_value())
if owner.host not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("the end-to-end test only runs against a local Postgres")
with create_engine(owner, isolation_level="AUTOCOMMIT").connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "docforge_e2e" WITH (FORCE)'))
    conn.execute(text('CREATE DATABASE "docforge_e2e"'))
print(owner.set(database="docforge_e2e").render_as_string(hide_password=False))
print(app.set(database="docforge_e2e").render_as_string(hide_password=False))
PY
)"
CREATED=1
export MIGRATION_DATABASE_URL="$(echo "$URLS" | sed -n 1p)"
export DATABASE_URL="$(echo "$URLS" | sed -n 2p)"
export PIPELINE_FACTORY="docforge.wiring:build_replay_pipelines"
# The webhook test sends to a receiver on this machine (refused in production).
export WEBHOOK_ALLOW_LOCAL=true
# Everything is replayed. E2E_RECORD=1 (with GEMINI_API_KEY) records what chat asks for.
if [ "${E2E_RECORD:-0}" = "1" ]; then
  export CHAT_RECORD=1
else
  export GEMINI_API_KEY=""
fi

uv run alembic upgrade head >/dev/null
uv run python -m docforge.db.roles >/dev/null
echo "246810" | uv run python -m docforge.review add-reviewer --admin --name "E2E Reviewer" --email e2e@example.com

uv run uvicorn docforge.api.main:create_default_app --factory --host 127.0.0.1 --port "$API_PORT" --no-proxy-headers >"$LOGS/api.log" 2>&1 &
PIDS+=($!)
uv run python -m docforge.worker >"$LOGS/worker.log" 2>&1 &
PIDS+=($!)

(cd web && npm run build >"$LOGS/web-build.log" 2>&1)
(cd web && DOCFORGE_API_URL="http://127.0.0.1:${API_PORT}" exec npx next start -p "$WEB_PORT" -H 127.0.0.1 >"$LOGS/web.log" 2>&1) &
PIDS+=($!)

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${API_PORT}/healthz" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:${WEB_PORT}/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

status=0
(cd web && E2E_BASE_URL="http://127.0.0.1:${WEB_PORT}" E2E_API_URL="http://127.0.0.1:${API_PORT}" npx playwright test "$@") || status=$?
if [ "$status" -ne 0 ]; then
  echo "--- api log ---"; tail -40 "$LOGS/api.log"
  echo "--- worker log ---"; tail -40 "$LOGS/worker.log"
  echo "--- web log ---"; tail -40 "$LOGS/web.log"
fi
exit "$status"
