#!/bin/sh
set -eu
umask 077
backup_dir=/var/backups/jobping
compose_file=${JOBPING_BACKUP_COMPOSE_FILE:-/srv/jobping/deploy/compose.infra.yml}
if [ ! -f "$compose_file" ]; then compose_file=/opt/jobping/deploy/compose.production.yml; fi
mkdir -p "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
temporary="$backup_dir/$stamp.dump.tmp"
trap 'rm -f "$temporary"' EXIT
docker compose -p jobping -f "$compose_file" exec -T postgres \
    pg_dump -p 5543 -U jobping -d jobping -Fc > "$temporary"
test -s "$temporary"
mv "$temporary" "$backup_dir/$stamp.dump"
docker compose -p jobping -f "$compose_file" exec -T postgres \
    pg_restore --list < "$backup_dir/$stamp.dump" > /dev/null
find "$backup_dir" -maxdepth 1 -type f -name '*.dump' -mtime +14 -delete
