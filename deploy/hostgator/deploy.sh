#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
cd "$REPOSITORY_DIR"
# This file holds public routing/database metadata only. Never source shell code from it.
API_DOMAIN=$(python3 -c 'import json; from urllib.parse import urlsplit; print(urlsplit(json.load(open("/etc/proace/config.json"))["FRONTEND_API_ORIGIN"]).hostname or "")')
POSTGRES_USER=$(python3 -c 'import json; print(json.load(open("/etc/proace/config.json"))["POSTGRES_USER"])')
POSTGRES_DB=$(python3 -c 'import json; print(json.load(open("/etc/proace/config.json"))["POSTGRES_DB"])')
export API_DOMAIN POSTGRES_USER POSTGRES_DB
[ -n "$API_DOMAIN" ] || { echo "Configure the public API origin." >&2; exit 1; }
# Refuse disk-backed runtime credentials. Agent owns restricted files on this mount.
[ "$(findmnt -n -o FSTYPE --target /run/proace-vault)" = tmpfs ] || { echo "Runtime secrets require the configured tmpfs mount." >&2; exit 1; }
for file in runtime.json postgres-password redis.conf minio-user minio-password; do
  test -s "/run/proace-vault/api/$file" || { echo "Vault Agent has not rendered required runtime files." >&2; exit 1; }
done
compose() { docker compose --env-file /dev/null -f deploy/hostgator/runtime.compose.yaml "$@"; }
docker network inspect commerce-network >/dev/null 2>&1 || docker network create commerce-network >/dev/null
API_IMAGE=$(python3 scripts/release_artifact.py image api)
STREAM_IMAGE=$(python3 scripts/release_artifact.py image stream)
export API_IMAGE STREAM_IMAGE
# Images are already reviewed/loaded. No package installation or source rebuild occurs here.
compose pull nginx
compose up -d --wait postgres redis minio elasticsearch
# Quiesce the previous release before changing its schema. Dependencies and data stay online.
# Match the release coordinator service set; stop drains workers before API migration.
compose stop --timeout 60 nginx api notification-worker media-worker notification-stream celery-beat
compose run --rm --no-deps --pull never api python manage.py migrate --noinput
compose run --rm --no-deps --pull never api python manage.py collectstatic --noinput
compose up -d --wait --no-build --pull never api notification-worker media-worker notification-stream celery-beat nginx
compose exec -T api python manage.py rebuild_product_index
docker compose --env-file /dev/null -f deploy/hostgator/compose.yaml up -d
compose ps
curl --fail --silent --show-error --retry 12 --retry-delay 5 "https://$API_DOMAIN/health/ready/" >/dev/null
echo "HostGator Vault runtime deployment completed."
