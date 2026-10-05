#!/usr/bin/env bash
# Sets up the demo host (Amazon Linux 2023, arm64) from nothing: Docker, the stack's settings
# from SSM Parameter Store, the images from ECR, the database, the demo data, and timers for
# the nightly reset and the ops check. Run by the instance's user data; safe to run again.
#
# Needs: the instance role (SSM read of /docforge/demo/*, ECR pull, CloudWatch
# put-metric-data) and this directory at /opt/docforge.
set -euo pipefail

cd "${DOCFORGE_DIR:-/opt/docforge}"
TOKEN="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
REGION="$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)"
export AWS_DEFAULT_REGION="$REGION"

# Docker and the Compose plugin (pinned).
COMPOSE_VERSION="v5.5.0"  # the version this was tested with
if ! command -v docker >/dev/null; then
  dnf install -y docker
  systemctl enable --now docker
fi
if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins
  base="https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/docker-compose-linux-aarch64" "$base/docker-compose-linux-aarch64"
  curl -fsSL -o "$tmp/docker-compose-linux-aarch64.sha256" "$base/docker-compose-linux-aarch64.sha256"
  (cd "$tmp" && sha256sum -c docker-compose-linux-aarch64.sha256)  # stop here if it differs
  install -m 0755 "$tmp/docker-compose-linux-aarch64" /usr/local/lib/docker/cli-plugins/docker-compose
fi

# Settings: three SecureStrings, readable only by root (see env.example).
umask 077
for part in compose:.env app:app.env owner:owner.env; do
  aws ssm get-parameter --name "/docforge/demo/${part%%:*}" --with-decryption \
    --query Parameter.Value --output text > "${part#*:}"
done
umask 022

# Images.
APP_IMAGE="$(grep '^APP_IMAGE=' .env | cut -d= -f2-)"
REGISTRY="${APP_IMAGE%%/*}"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
docker compose pull --quiet

# Database, then the services, then the demo data.
docker compose up -d --wait postgres
docker compose run --rm admin alembic upgrade head
docker compose run --rm admin python -m docforge.db.roles
docker compose up -d --wait
docker compose run --rm admin python -m docforge.demo seed

# Timers: the nightly reset and the ops check every five minutes.
install -m 0644 systemd/docforge-*.service systemd/docforge-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now docforge-reset.timer docforge-ops.timer
echo "DocForge demo is up."
