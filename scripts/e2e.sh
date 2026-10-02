#!/usr/bin/env bash
# End-to-end: a fresh database, the real API and worker on recorded parses and model replies
# (no key, no network), the built review app, and Playwright driving a browser through it.
# Needs `make up` (Postgres and MinIO). Everything started here is stopped on exit.
set -euo pipefail

cd "$(dirname "$0")/.."
API_PORT="${E2E_API_PORT:-8011}"
WEB_PORT="${E2E_WEB_PORT:-3011}"
LOGS="$(mktemp -d)"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  uv run python - <<'PY' >/dev/null 2>&1 || true
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from docforge.config import get_settings
url = make_url(get_settings().database_url.get_secret_value())
with create_engine(url, isolation_level="AUTOCOMMIT").connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "docforge_e2e" WITH (FORCE)'))
PY
}
trap cleanup EXIT

E2E_URL="$(uv run python - <<'PY'
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from docforge.config import get_settings
url = make_url(get_settings().database_url.get_secret_value())
if url.host not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("the end-to-end test only runs against a local Postgres")
with create_engine(url, isolation_level="AUTOCOMMIT").connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "docforge_e2e" WITH (FORCE)'))
    conn.execute(text('CREATE DATABASE "docforge_e2e"'))
print(url.set(database="docforge_e2e").render_as_string(hide_password=False))
PY
)"
export DATABASE_URL="$E2E_URL"
export PIPELINE_FACTORY="docforge.wiring:build_replay_pipelines"
export CORS_ORIGINS="http://127.0.0.1:${WEB_PORT}"
export GEMINI_API_KEY=""

uv run alembic upgrade head >/dev/null
echo "246810" | uv run python -m docforge.review add-reviewer --name "E2E Reviewer" --email e2e@example.com

uv run uvicorn docforge.api.main:create_default_app --factory --host 127.0.0.1 --port "$API_PORT" >"$LOGS/api.log" 2>&1 &
PIDS+=($!)
uv run python -m docforge.worker >"$LOGS/worker.log" 2>&1 &
PIDS+=($!)

(cd web && NEXT_PUBLIC_API_URL="http://127.0.0.1:${API_PORT}" npm run build >"$LOGS/web-build.log" 2>&1)
(cd web && npx next start -p "$WEB_PORT" -H 127.0.0.1 >"$LOGS/web.log" 2>&1) &
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
fi
exit "$status"
