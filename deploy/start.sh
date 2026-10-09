#!/bin/sh
# The image's one entry point. DOCFORGE_ROLE picks what this container is, for hosts that run
# one image as several services but cannot set a command per service (Railway). Compose sets
# its own commands and never needs it; with no role set, this is the API, as before.
set -eu

case "${DOCFORGE_ROLE:-api}" in
  api)
    # Only the web app (and Caddy, under Compose) reach this port, so the client address they
    # forward is trusted.
    exec uvicorn docforge.api.main:create_default_app --factory --host 0.0.0.0 --port 8000 \
      --proxy-headers --forwarded-allow-ips '*'
    ;;
  worker)
    exec python -m docforge.worker
    ;;
  converter)
    exec python -m docforge.conversion_service
    ;;
  admin)
    # One run per release: the schema, the application's restricted login, and the demo's
    # organisation and documents (both steps are safe to repeat). Needs the owner's URL in
    # MIGRATION_DATABASE_URL; the API and worker never hold it.
    alembic upgrade head
    python -m docforge.db.roles
    if [ -n "${DEMO_PIN:-}" ]; then
      python -m docforge.demo seed
    fi
    # An owner's first organisation and admin on a host with no shell into its containers. Set
    # the four OWNER_* variables for one release, then remove them; a repeat run only reports
    # that both exist. The PIN goes in on standard input, never on a command line.
    if [ -n "${OWNER_ORG:-}" ] && [ -n "${OWNER_EMAIL:-}" ] && [ -n "${OWNER_PIN:-}" ]; then
      python -m docforge.admin create-tenant "$OWNER_ORG" || true
      printf '%s\n' "$OWNER_PIN" | python -m docforge.review add-reviewer --admin \
        --tenant "$OWNER_ORG" --name "${OWNER_NAME:-Owner}" --email "$OWNER_EMAIL" || true
    fi
    echo "admin: done"
    ;;
  *)
    echo "unknown DOCFORGE_ROLE: ${DOCFORGE_ROLE}" >&2
    exit 64
    ;;
esac
