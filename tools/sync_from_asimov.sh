#!/usr/bin/env bash
set -euo pipefail

HOST="${SEERR_STACK_HOST:-justin@asimov.local}"
REMOTE_COMPOSE="${SEERR_STACK_REMOTE_COMPOSE:-/home/docker/volumes/portainer_data/_data/compose/6/docker-compose.yml}"
REMOTE_SCRIPT="${SEERR_STACK_REMOTE_SCRIPT:-/opt/media/scripts/seerr_queue_rescue.py}"
REMOTE_CRON="${SEERR_STACK_REMOTE_CRON:-/etc/cron.d/seerr-queue-rescue}"

cd "$(dirname "$0")/.."

tmpdir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

ssh "$HOST" "sudo sed -n '1,400p' '$REMOTE_COMPOSE'" > "$tmpdir/docker-compose.yml"
ssh "$HOST" "sudo sed -n '1,240p' '$REMOTE_CRON'" > "$tmpdir/seerr-queue-rescue"
ssh "$HOST" "sudo sed -n '1,1000p' '$REMOTE_SCRIPT'" > "$tmpdir/seerr_queue_rescue.py"

cp "$tmpdir/docker-compose.yml" docker-compose.yml
cp "$tmpdir/seerr-queue-rescue" cron/seerr-queue-rescue
cp "$tmpdir/seerr_queue_rescue.py" scripts/seerr_queue_rescue.py

chmod 0755 scripts/seerr_queue_rescue.py tools/sync_from_asimov.sh

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --source . --no-git --redact
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$PWD:/repo" ghcr.io/gitleaks/gitleaks:latest detect --source /repo --no-git --redact
else
  echo "ERROR: gitleaks is not installed and docker is unavailable" >&2
  exit 1
fi

if git diff --quiet -- . && [ -z "$(git status --porcelain -- docker-compose.yml cron/seerr-queue-rescue scripts/seerr_queue_rescue.py)" ]; then
  echo "No stack changes to commit."
  exit 0
fi

git add docker-compose.yml cron/seerr-queue-rescue scripts/seerr_queue_rescue.py
git commit -m "Sync Seerr stack from Asimov"
