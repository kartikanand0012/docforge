#!/usr/bin/env bash
# Sets up the demo host (Amazon Linux 2023, arm64) from nothing: Docker, the stack's settings
# from SSM Parameter Store, the images from ECR, the database, the demo data, and timers for
# the nightly reset and the ops check. Run by the instance's user data; safe to run again.
#
# Needs: the instance role (SSM read of /docforge/demo/*, ECR pull, S3 on the bucket,
# CloudWatch put-metric-data) and this directory at /opt/docforge.
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
  curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64"
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

# Settings: one SecureString holding the whole .env, readable only by root.
umask 077
aws ssm get-parameter --name /docforge/demo/env --with-decryption \
  --query Parameter.Value --output text > .env
umask 022

# Images.
APP_IMAGE="$(grep '^APP_IMAGE=' .env | cut -d= -f2-)"
REGISTRY="${APP_IMAGE%%/*}"
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
docker compose pull --quiet

# Database, then the services, then the demo data.
docker compose up -d --wait postgres
docker compose run --rm api alembic upgrade head
docker compose run --rm api python -m docforge.db.roles
docker compose up -d --wait
docker compose run --rm api python -m docforge.demo seed

# Timers: the nightly reset and the ops check every five minutes.
install -m 0644 systemd/docforge-*.service systemd/docforge-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now docforge-reset.timer docforge-ops.timer
echo "DocForge demo is up."
