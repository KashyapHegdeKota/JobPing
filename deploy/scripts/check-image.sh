#!/usr/bin/env bash
# Disposable local CI services only. Does not use production configuration or volumes.
set -Eeuo pipefail
image=$1 sha=$2
prefix="jobping-image-test-${GITHUB_RUN_ID:-$$}"
# shellcheck disable=SC2317
cleanup() { docker rm -f "$prefix-api" "$prefix-pg" "$prefix-redis" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$prefix-pg" --network host -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test-only \
    -e POSTGRES_DB=jobping_image postgres:16-alpine postgres -p 15443 -c listen_addresses=127.0.0.1 >/dev/null
docker run -d --name "$prefix-redis" --network host redis:7-alpine \
    redis-server --bind 127.0.0.1 --port 16381 >/dev/null
ready=false
for ((i=0; i<30; i++)); do
    # TCP excludes PostgreSQL's temporary Unix-only initialization server.
    if docker exec "$prefix-pg" pg_isready -h 127.0.0.1 -p 15443 -U test >/dev/null; then ready=true; break; fi
    sleep 1
done
if [[ $ready != true ]]; then docker logs --tail 80 "$prefix-pg"; exit 1; fi
envs=(-e DATABASE_URL=postgresql+psycopg://test:test-only@127.0.0.1:15443/jobping_image \
    -e REDIS_URL=redis://127.0.0.1:16381/0 -e NOTIFICATIONS_SEND_ENABLED=false -e APP_COLOR=blue)
docker run --rm --network host "${envs[@]}" "$image" alembic upgrade head
docker run -d --name "$prefix-api" --network host "${envs[@]}" "$image" \
    uvicorn app.main:app --host 127.0.0.1 --port 18001 >/dev/null
for ((i=0; i<30; i++)); do
    if python3 deploy/scripts/smoke.py http://127.0.0.1:18001 "$sha" blue >/dev/null 2>&1; then
        echo 'Exact production image passes migration/readiness/version/API smoke'; exit 0
    fi
    sleep 2
done
docker logs --tail 80 "$prefix-api"
exit 1
