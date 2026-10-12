#!/usr/bin/env bash
set -euo pipefail
exec sudo -n /srv/jobping/controller/receive.sh "${SSH_ORIGINAL_COMMAND:-}"
