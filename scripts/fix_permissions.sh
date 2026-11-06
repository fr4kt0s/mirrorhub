#!/usr/bin/env bash
# Re-apply ownership on the writable volumes, then prove the export can write.
set -euo pipefail

COMPOSE_FILES=(-f docker-compose.yml)
[[ -f docker-compose.override.yml ]] && COMPOSE_FILES+=(-f docker-compose.override.yml)

docker compose "${COMPOSE_FILES[@]}" up -d --force-recreate init-volumes
docker compose "${COMPOSE_FILES[@]}" wait init-volumes || true

for service in worker-export worker-debmirror worker-rsync worker-wget; do
  printf '%-20s ' "$service"
  docker compose "${COMPOSE_FILES[@]}" exec -T "$service" sh -c '
    id -un
    mkdir -p /export/.probe-dir && rmdir /export/.probe-dir
    test -w /data && echo "  /data writable"
    test -w /export && echo "  /export writable"
  '
done

echo "OK"