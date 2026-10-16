#!/usr/bin/env bash
# Root-owned bootstrap receiver: the deployment key cannot request a shell.
set -Eeuo pipefail
umask 077
[[ $# == 1 && $1 =~ ^deploy\ ([0-9a-f]{40})\ (sha256:[0-9a-f]{64})$ ]] || { echo 'Invalid deployment command' >&2; exit 2; }
sha=${BASH_REMATCH[1]} digest=${BASH_REMATCH[2]}
root=/srv/jobping
mkdir -p "$root/releases" "$root/state"
exec 8>"$root/state/receive.lock"
flock -n 8 || { echo 'Another release transfer owns the lock' >&2; exit 1; }
temporary=$(mktemp -d "$root/releases/.receive.XXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
# Short-lived Actions token travels over verified SSH, never argv, logs or permanent config.
IFS= read -r token
[[ -n $token && ${#token} -lt 2048 ]] || exit 2
export DOCKER_CONFIG="$temporary/docker-auth"
printf '%s' "$token" | docker login ghcr.io -u KashyapHegdeKota --password-stdin >/dev/null 2>&1
unset token
cat > "$temporary/release.tar.gz"
python3 - "$temporary/release.tar.gz" "$temporary/source" <<'PY'
import sys, tarfile
from pathlib import Path
with tarfile.open(sys.argv[1], "r:gz") as archive:
    members = archive.getmembers()
    if len(members) > 100 or sum(item.size for item in members) > 2_000_000:
        raise SystemExit("Oversized deployment bundle")
    for item in members:
        path = Path(item.name)
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "deploy" or not (item.isfile() or item.isdir()):
            raise SystemExit("Invalid deployment bundle member")
    archive.extractall(sys.argv[2], filter="data")
PY
release="$root/releases/$sha"
if [[ ! -d $release ]]; then
    mv -- "$temporary/source" "$release"
    chmod -R go-w "$release"
fi
bash "$release/deploy/scripts/deploy.sh" deploy "$sha" "$digest"
# Stable operator entry points follow the successfully activated controller.
ln -s "$release/deploy" "$root/deploy.next"
mv -Tf -- "$root/deploy.next" "$root/deploy"
