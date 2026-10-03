#!/usr/bin/env bash
# Every five minutes: `python -m docforge.ops check`, its status (0 ok, 1 warning, 2 critical)
# sent to CloudWatch, where an alarm emails the owner. Its JSON goes to the journal.
set -uo pipefail

cd "${DOCFORGE_DIR:-/opt/docforge}" || exit 1
# As the owner (counts across organisations), in a container that runs and is gone.
docker compose run --rm -T admin python -m docforge.ops check
status=$?
aws cloudwatch put-metric-data --namespace DocForge --metric-name OpsStatus \
  --dimensions Stack=demo --value "$status"
exit 0
