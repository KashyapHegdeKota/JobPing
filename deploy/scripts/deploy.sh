#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# Only operator-owned configuration is sourced, never private application dotenv.
if [[ -f ${JOBPING_DEPLOY_CONFIG:-/etc/jobping/deployment.conf} ]]; then
    # shellcheck disable=SC1090
    source "${JOBPING_DEPLOY_CONFIG:-/etc/jobping/deployment.conf}"
fi
ROOT=${JOBPING_ROOT:-/srv/jobping}
STATE="$ROOT/state"
REPOSITORY=${IMAGE_REPOSITORY:-ghcr.io/kashyaphegdekota/jobping}
HOST=${PRODUCTION_HOST:-api.jobping.website}
UPSTREAM=${UPSTREAM_FILE:-/etc/nginx/jobping/upstream.conf}
NGINX=${NGINX_CONFIG:-/etc/nginx/nginx.conf}
ATTEMPTS=${READINESS_ATTEMPTS:-30}
INTERVAL=${READINESS_INTERVAL:-2}
NOTIFICATIONS=${NOTIFICATION_WORKER_ENABLED:-false}
STAGE=initialization TARGET=none PREVIOUS=none SWITCHED=false WORKERS_TOUCHED=false TARGET_TOUCHED=false
mkdir -p "$STATE"
exec 9>"$STATE/deploy.lock"
flock -n 9 || { echo 'Another deployment owns the lock' >&2; exit 1; }
log() { printf '%s stage=%s %s\n' "$(date -u +%FT%TZ)" "$STAGE" "$*"; }
die() { log "$*" >&2; exit 1; }
valid_color() { [[ $1 == blue || $1 == green || $1 == legacy || $1 == none ]]; }
get() { awk -v key="$2" 'index($0,key"=")==1 {print substr($0,length(key)+2)}' "$1"; }
atomic() { printf '%s\n' "$2" > "$1.tmp"; mv -f -- "$1.tmp" "$1"; }
active() {
    [[ -f $STATE/active-color ]] || die 'Missing active-color; bootstrap explicitly before deploying'
    local color; color=$(cat "$STATE/active-color")
    valid_color "$color" || die 'Malformed active-color'
    printf '%s' "$color"
}
port() { case $1 in blue) echo 8001;; green) echo 8002;; legacy) echo 8020;; *) return 1;; esac; }
meta() { get "$STATE/$1.env" "$2"; }
api() {
    local color=$1; shift
    JOBPING_IMAGE=$(meta "$color" IMAGE) JOBPING_COLOR=$color JOBPING_PORT=$(port "$color") \
        docker compose -p "jobping-$color" -f "$(meta "$color" RELEASE)/deploy/compose.api.yml" "$@"
}
workers() {
    local file=$1; shift
    [[ $(get "$file" COLOR) != none ]] || return 0
    if [[ $(get "$file" COLOR) == legacy ]]; then
        docker compose -p jobping -f /opt/jobping/deploy/compose.production.yml --profile notifications "$@" scheduler notifications
    else
        local profiles=()
        if [[ $NOTIFICATIONS == true || $1 == stop || $1 == ps ]]; then profiles=(--profile notifications); fi
        JOBPING_IMAGE=$(get "$file" IMAGE) JOBPING_ROOT=$ROOT \
            docker compose -p jobping-workers -f "$(get "$file" RELEASE)/deploy/compose.workers.yml" "${profiles[@]}" "$@"
    fi
}
smoke() {
    local color=$1 mode=${2:-direct} base; base="http://127.0.0.1:$(port "$color")"
    local options=(); [[ $mode != proxy ]] || { base="https://$HOST"; options=(--proxy); }
    python3 "$HERE/smoke.py" "$base" "$(meta "$color" COMMIT_SHA)" "$color" "${options[@]}"
}
wait_ready() {
    local color=$1 mode=${2:-direct}
    for ((attempt=1; attempt<=ATTEMPTS; attempt++)); do
        if smoke "$color" "$mode" >/dev/null 2>&1; then log "readiness/smoke passed color=$color via=$mode"; return 0; fi
        sleep "$INTERVAL"
    done
    log "readiness/smoke exhausted color=$color via=$mode"; return 1
}
switch_proxy() {
    local color=$1 staging candidate
    staging=$(mktemp -d "$STATE/nginx.XXXXXX")
    candidate="$staging/upstream.conf"
    printf 'upstream jobping_backend { server 127.0.0.1:%s; }\n' "$(port "$color")" > "$candidate"
    if ! python3 "$HERE/proxy_candidate.py" "$NGINX" "$UPSTREAM" "$candidate" "$staging/config" \
        || ! nginx -t -c "$staging/config/$(basename "$NGINX")"; then
        rm -rf -- "$staging"; return 1
    fi
    cp -- "$candidate" "$UPSTREAM.tmp"
    mv -f -- "$UPSTREAM.tmp" "$UPSTREAM"
    rm -rf -- "$staging"
    nginx -t -c "$NGINX" && systemctl reload nginx
}
write_metadata() {
    local color=$1 sha=$2 image=$3 release=$4
    printf 'COLOR=%s\nCOMMIT_SHA=%s\nIMAGE=%s\nRELEASE=%s\nDEPLOYED_AT=%s\n' \
        "$color" "$sha" "$image" "$release" "$(date -u +%FT%TZ)" > "$STATE/$color.env.tmp"
    mv -- "$STATE/$color.env.tmp" "$STATE/$color.env"
}
wait_workers() {
    local file=$1 expected=1; [[ $NOTIFICATIONS != true ]] || expected=2
    [[ $(get "$file" COLOR) != legacy ]] || return 0
    for ((attempt=1; attempt<=ATTEMPTS; attempt++)); do
        local ids healthy=0; ids=$(workers "$file" ps -q)
        while IFS= read -r id; do
            [[ -z $id ]] || [[ $(docker inspect -f '{{.State.Health.Status}}' "$id") != healthy ]] || healthy=$((healthy+1))
        done <<< "$ids"
        if [[ $healthy == "$expected" ]]; then return 0; fi
        sleep "$INTERVAL"
    done
    return 1
}
cut_workers() {
    local color=$1
    cp -- "$STATE/workers.env" "$STATE/workers-before.env"
    WORKERS_TOUCHED=true
    # Stop and confirm exit before starting a new singleton generation.
    workers "$STATE/workers-before.env" stop --timeout 180
    local ids; ids=$(workers "$STATE/workers-before.env" ps -q --status running)
    [[ -z $ids ]] || return 1
    workers "$STATE/$color.env" up -d --remove-orphans
    wait_workers "$STATE/$color.env"
    cp -- "$STATE/$color.env" "$STATE/workers.env.tmp"
    mv -- "$STATE/workers.env.tmp" "$STATE/workers.env"
}
restore_workers() {
    [[ $WORKERS_TOUCHED == true ]] || return 0
    workers "$STATE/$TARGET.env" stop --timeout 180 || return 1
    workers "$STATE/workers-before.env" up -d || return 1
    wait_workers "$STATE/workers-before.env" || return 1
    cp -- "$STATE/workers-before.env" "$STATE/workers.env"
}
failure() {
    local result=$1
    trap - ERR INT TERM HUP
    log "FAILED color=$TARGET previous=$PREVIOUS traffic_changed=$SWITCHED (database is never downgraded)" >&2
    # Read-only/preflight failures must not clear another operation's recovery journal.
    if [[ $TARGET_TOUCHED != true && $SWITCHED != true && $WORKERS_TOUCHED != true ]]; then exit "$result"; fi
    local restored=true
    if [[ $SWITCHED == true ]]; then
        if [[ $PREVIOUS != none ]] && wait_ready "$PREVIOUS" && switch_proxy "$PREVIOUS" && wait_ready "$PREVIOUS" proxy; then
            atomic "$STATE/active-color" "$PREVIOUS"
            log "Traffic restored to $PREVIOUS"
        else restored=false; log 'AUTOMATIC TRAFFIC RESTORE FAILED; pending journal retained' >&2; fi
    fi
    restore_workers || { restored=false; log 'WORKER RESTORE FAILED; operator recovery required' >&2; }
    if [[ $TARGET == blue || $TARGET == green ]]; then
        api "$TARGET" logs --tail 80 >&2 || true
        # Never stop a target that may still be serving traffic after a failed restore.
        if [[ $restored == true && $TARGET_TOUCHED == true ]]; then
            api "$TARGET" stop || true
            if [[ -f $STATE/target-before.env ]]; then
                cp -- "$STATE/target-before.env" "$STATE/$TARGET.env"
                if api "$TARGET" up -d --no-deps api && wait_ready "$TARGET"; then
                    log "Standby restored color=$TARGET commit=$(meta "$TARGET" COMMIT_SHA)"
                else restored=false; log 'STANDBY RESTORE FAILED' >&2; fi
            fi
        fi
    fi
    [[ $restored != true ]] || rm -f -- "$STATE/pending"
    exit "$result"
}
trap 'failure $?' ERR
trap 'failure 130' INT
trap 'failure 143' TERM
trap 'failure 129' HUP
status() {
    log "active=$(active) proxy=$(cat "$UPSTREAM")"
    for color in blue green legacy; do
        [[ -f $STATE/$color.env ]] || continue
        cat "$STATE/$color.env"
        if smoke "$color" >/dev/null 2>&1; then echo "HEALTH=ready"; else echo 'HEALTH=unavailable'; fi
    done
    [[ ! -f $STATE/workers.env ]] || { echo 'WORKER/SCHEDULER VERSION:'; cat "$STATE/workers.env"; workers "$STATE/workers.env" ps; }
    [[ ! -f $STATE/pending ]] || { echo 'RECOVERY REQUIRED:'; cat "$STATE/pending"; }
}
cleanup() {
    # Only this application's labeled, unreferenced image IDs. Docker refuses IDs in use.
    local protected="" color id
    for color in blue green legacy workers; do
        if [[ -f $STATE/$color.env ]]; then
            id=$(docker image inspect -f '{{.Id}}' "$(meta "$color" IMAGE)") || return 1
            protected+=" $id"
        fi
    done
    while IFS= read -r id; do
        [[ -n $id ]] || continue
        [[ " $protected " != *" $id "* ]] || continue
        if [[ -z $(docker ps -aq --filter "ancestor=$id") ]]; then docker image rm "$id" || true; fi
    done < <(docker image ls --no-trunc --filter 'label=org.opencontainers.image.source=https://github.com/KashyapHegdeKota/JobPing' --format '{{.ID}}' | sort -u)
}
command=${1:-}; shift || true
case $command in
    status) [[ $# == 0 ]] || die 'status takes no arguments'; status; exit 0;;
    recover)
        [[ $# == 0 && -f $STATE/pending ]] || die 'No pending recovery'
        PREVIOUS=$(get "$STATE/pending" PREVIOUS)
        TARGET=$(get "$STATE/pending" TARGET)
        if ! valid_color "$PREVIOUS" || ! valid_color "$TARGET" || [[ $PREVIOUS == none ]]; then die 'Invalid recovery journal'; fi
        wait_ready "$PREVIOUS"
        switch_proxy "$PREVIOUS"
        wait_ready "$PREVIOUS" proxy
        if [[ -f $STATE/workers-before.env ]]; then
            WORKERS_TOUCHED=true
            restore_workers
        fi
        if [[ -f $STATE/target-before.env && $TARGET != legacy ]]; then
            cp -- "$STATE/target-before.env" "$STATE/$TARGET.env"
            api "$TARGET" up -d --no-deps api
            wait_ready "$TARGET"
        fi
        atomic "$STATE/active-color" "$PREVIOUS"
        rm -f -- "$STATE/pending"
        log "Recovered active=$PREVIOUS"; exit 0;;
    deploy|rollback) ;;
    *) die 'Usage: deploy.sh deploy <40-character SHA> <sha256:digest> | rollback | status';;
esac
[[ ! -f $STATE/pending ]] || die 'Unfinished cutover: inspect status and recover pending state before another deployment'
PREVIOUS=$(active)
if [[ $command == deploy ]]; then
    [[ $# == 2 && $1 =~ ^[0-9a-f]{40}$ && $2 =~ ^sha256:[0-9a-f]{64}$ ]] || die 'Invalid SHA/digest'
    SHA=$1 IMAGE="$REPOSITORY@$2" RELEASE="$ROOT/releases/$1"
    if [[ $PREVIOUS != none && $(meta "$PREVIOUS" COMMIT_SHA) == "$SHA" ]]; then
        [[ $(meta "$PREVIOUS" IMAGE) == "$IMAGE" ]] || die 'SHA is already active with a different digest'
        log "Already active commit=$SHA color=$PREVIOUS"; exit 0
    fi
    [[ $PREVIOUS != blue ]] && TARGET=blue || TARGET=green
    STAGE=pull; log "commit=$SHA image=$IMAGE target=$TARGET previous=$PREVIOUS"
    docker pull "$IMAGE"
    [[ $(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE") == "$SHA" ]] || die 'Image commit label mismatch'
    [[ -f $RELEASE/deploy/compose.api.yml && -f $RELEASE/deploy/compose.workers.yml ]] || die 'Release bundle missing'
    rm -f -- "$STATE/target-before.env"
    [[ ! -f $STATE/$TARGET.env ]] || cp -- "$STATE/$TARGET.env" "$STATE/target-before.env"
    cp -- "$STATE/workers.env" "$STATE/workers-before.env"
    printf 'PREVIOUS=%s\nTARGET=%s\n' "$PREVIOUS" "$TARGET" > "$STATE/pending"
    write_metadata "$TARGET" "$SHA" "$IMAGE" "$RELEASE"
    TARGET_TOUCHED=true
    STAGE=migration
    # Once per deployment, serialized by flock. Only reviewed expand/contract upgrades.
    api "$TARGET" run --rm --no-deps api alembic upgrade head
    STAGE=startup; api "$TARGET" up -d --no-deps api
else
    [[ $# == 0 ]] || die 'rollback takes no arguments'
    [[ -f $STATE/previous-color ]] || die 'No previous deployment'
    TARGET=$(cat "$STATE/previous-color")
    if ! valid_color "$TARGET" || [[ $TARGET == none || $TARGET == "$PREVIOUS" ]]; then die 'Invalid previous deployment'; fi
fi
STAGE=readiness; wait_ready "$TARGET"
if [[ $command == rollback ]]; then
    rm -f -- "$STATE/target-before.env"
    cp -- "$STATE/workers.env" "$STATE/workers-before.env"
    printf 'PREVIOUS=%s\nTARGET=%s\n' "$PREVIOUS" "$TARGET" > "$STATE/pending"
fi
STAGE=proxy
# Set before reload: a failed reload must restore the previous on-disk configuration.
SWITCHED=true
switch_proxy "$TARGET"
STAGE=verification; wait_ready "$TARGET" proxy
atomic "$STATE/active-color" "$TARGET"
STAGE=workers; cut_workers "$TARGET"
atomic "$STATE/previous-color" "$PREVIOUS"
rm -f -- "$STATE/pending"
STAGE=complete; log "SUCCESS active=$TARGET previous=$PREVIOUS commit=$(meta "$TARGET" COMMIT_SHA)"
cleanup || log 'Image cleanup deferred; deployment remains successful'
