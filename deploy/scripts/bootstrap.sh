#!/usr/bin/env bash
# Explicit one-time adoption/bootstrap. Run as an operator after reviewing docs.
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run bootstrap as root' >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=/srv/jobping
[[ -f /etc/jobping/backend.env && -d /etc/jobping/firebase ]] || { echo 'Private application configuration required' >&2; exit 1; }
mkdir -p "$root/controller" "$root/state" "$root/releases" "$root/worker-state" /etc/nginx/jobping
chown 10001:10001 "$root/worker-state"
chmod 750 "$root/worker-state"
install -m 755 "$here/receive.sh" "$root/controller/receive.sh"
install -m 755 "$here/ssh-entry.sh" /usr/local/bin/jobping-deploy-ssh
install -m 750 "$here/../backup.sh" "$root/controller/backup.sh"
install -m 644 "$here/../jobping-backup.service" /etc/systemd/system/jobping-backup.service
install -m 644 "$here/../jobping-backup.timer" /etc/systemd/system/jobping-backup.timer
systemctl daemon-reload
systemctl enable --now jobping-backup.timer
if ! id jobping-deploy >/dev/null 2>&1; then useradd -m -s /bin/bash jobping-deploy; fi
install -d -m 700 -o jobping-deploy -g jobping-deploy /home/jobping-deploy/.ssh
printf 'jobping-deploy ALL=(root) NOPASSWD: /srv/jobping/controller/receive.sh *\n' > /etc/sudoers.d/jobping-deploy
chmod 440 /etc/sudoers.d/jobping-deploy
visudo -cf /etc/sudoers.d/jobping-deploy
[[ -f /etc/jobping/deployment.conf ]] || install -m 600 "$here/../deployment.conf.example" /etc/jobping/deployment.conf
# shellcheck disable=SC1091
source /etc/jobping/deployment.conf
[[ ! -e $root/state/active-color ]] || { echo 'Already initialized; active state preserved'; exit 0; }
if [[ ${LEGACY_ADOPTION:-false} == true ]]; then
    docker inspect jobping-api-1 >/dev/null
    image=$(docker inspect -f '{{.Image}}' jobping-api-1)
    sha=$(cat /opt/jobping/DEPLOYED_REVISION)
    # Preserve the old image by immutable ID, even when its local tag changes.
    printf 'COLOR=legacy\nCOMMIT_SHA=%s\nIMAGE=%s\nRELEASE=/opt/jobping\nDEPLOYED_AT=%s\n' \
        "$sha" "$image" "$(date -u +%FT%TZ)" > "$root/state/legacy.env"
    cp "$root/state/legacy.env" "$root/state/workers.env"
    printf 'legacy\n' > "$root/state/active-color"
    printf 'upstream jobping_backend { server 127.0.0.1:8020; }\n' > /etc/nginx/jobping/upstream.conf
else
    printf 'none\n' > "$root/state/active-color"
    printf 'COLOR=none\n' > "$root/state/workers.env"
    printf 'upstream jobping_backend { server 127.0.0.1:8001; }\n' > /etc/nginx/jobping/upstream.conf
fi
echo 'Initialized. Install the reviewed Nginx upstream include and restricted deployment public key before enabling CI.'
