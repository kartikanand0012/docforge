#!/usr/bin/env bash
# The nightly reset of the public demo: visitors' corrections and signatures are cleared by
# re-creating the demo's own database and seeding it again. Only for the demo host: it acts
# on the `docforge-demo` Compose project's Postgres container and nothing else.
set -euo pipefail

cd "${DOCFORGE_DIR:-/opt/docforge}"
if ! docker compose ls --quiet | grep -qx docforge-demo; then
  echo "not the demo host (no docforge-demo stack); nothing reset" >&2
  exit 1
fi

docker compose stop api worker web
docker compose exec -T postgres psql -U docforge -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE IF EXISTS docforge WITH (FORCE)' \
  -c 'CREATE DATABASE docforge'
docker compose run --rm admin alembic upgrade head
docker compose run --rm admin python -m docforge.db.roles
docker compose up -d --wait api worker web
docker compose run --rm admin python -m docforge.demo seed
