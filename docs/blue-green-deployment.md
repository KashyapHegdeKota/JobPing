# One-VM blue-green deployment

JobPing retains Oracle ARM64, Nginx/Certbot HTTPS, host networking and its existing
loopback PostgreSQL 5543 / Redis 6381 services. API colors are loopback 8001 and
8002, each with one Uvicorn worker. The previous API stays running for immediate
rollback and existing WebSocket/SSE sessions until that color is reused. Clients
then reconnect normally. No production data or private configuration is copied
into a release bundle or image. Other applications on the VM are untouched.

## CI and exact artifacts

PRs run existing quality, browser and PostgreSQL/Redis/MCP acceptance checks plus
ShellCheck, Compose validation and a network-free controller failure simulation.
Pushes to the default branch `master` (also supports `main`) require **all** these
jobs before building one native ARM64 image. The built image runs its migrations
and read-only readiness/version/feed smoke checks against disposable services
before publication as `ghcr.io/kashyaphegdekota/jobping:<40-character SHA>`.
Deployment passes `github.sha` and the published `sha256` digest; the VM checks
the image revision label. It pulls by digest, never a mutable branch/tag.

The production job uses the `production` environment, serial deployment
concurrency, and verified SSH host keys. Push workflows are never cancelled by a
new push while deploying. PR workflows can be cancelled. Both the receiver and
controller have nonblocking VM locks. A trusted workflow transfers only `deploy/`
and a short-lived package-read Actions token over SSH. Registry credentials live
in a temporary root-only directory and are deleted when the receiver exits.
There is no permanent registry token on this VM. GitHub documents the
[Actions registry token](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## VM layout and first adoption

```text
/srv/jobping/controller/          root-owned SSH receiver
/srv/jobping/releases/<sha>/      immutable deployment configuration
/srv/jobping/deploy -> releases/<successful-sha>/deploy
/srv/jobping/state/               active/previous colors, metadata, lock, journal
/srv/jobping/worker-state/        UID 10001 singleton locks and heartbeats
/etc/jobping/backend.env         existing private production dotenv
/etc/jobping/firebase/           existing read-only credentials
/etc/jobping/deployment.conf     root-owned deployment settings
/etc/nginx/jobping/upstream.conf generated jobping_backend upstream
```

Prerequisites: Ubuntu with Docker Engine/Compose v2, Python 3.12+, Bash, flock,
Nginx and Certbot. Preserve existing DNS/TLS and unrelated virtual hosts. On a
new VM install these packages using the official Ubuntu/Docker instructions,
provision private configuration and create shared services with
`docker compose -f deploy/compose.infra.yml up -d`. The existing VM already has
them; **do not start another infrastructure project or recreate its volumes**.
The API and workers use host networking because this VM's bridge routing is
incomplete. Secrets and images never contain local candidate profiles.

1. Back up the database and current Nginx JobPing site before adoption. Take a
   fresh verified `jobping-backup.service` backup before migration releases.
2. Copy the reviewed `deploy/` directory to the VM. Configure root-only
   `/etc/jobping/deployment.conf` from `deploy/deployment.conf.example`, preserving
   whether notification delivery is currently opted in. `LEGACY_ADOPTION=true`
   records existing API 8020 and workers as the first fallback. On a new VM set
   it false. Run `sudo bash deploy/scripts/bootstrap.sh` once. Subsequent calls
   leave initialized state unchanged.
3. Add `include /etc/nginx/jobping/upstream.conf;` in the site's HTTP context and
   replace only JobPing's upstream with `proxy_pass http://jobping_backend;`.
   Preserve Certbot's TLS directives and current guards. Run `sudo nginx -t`
   and reload gracefully. This initial upstream still targets existing 8020.
   The template `deploy/nginx.conf` also retains request-protection dry-run rules;
   this feature does not activate the separate request-abuse rollout.
   The explicit adoption helper `sudo python3 deploy/scripts/adopt_nginx.py
   /etc/nginx/sites-enabled/jobping` backs up the site, stages/tests the complete
   Nginx tree and restores the original on reload failure.
4. Generate a dedicated Ed25519 key. Install its public key as:
   `restrict,command="/usr/local/bin/jobping-deploy-ssh" ssh-ed25519 ...`
   in `/home/jobping-deploy/.ssh/authorized_keys` (directory 0700, file 0600).
   The account is password locked, has no Docker group membership, and can sudo
   only the root-owned validating receiver. Docker control is root-equivalent;
   trusted production repository code is consequently deployment authority.
5. Save the private key in the GitHub `production` environment as `VM_SSH_KEY`;
   set `VM_HOST`, `VM_USER=jobping-deploy`, and `VM_KNOWN_HOSTS`. Optional
   `VM_SSH_PORT` defaults to 22. Obtain the server's public host key through an
   already verified administrator connection, compare its fingerprint, and pin
   the full known_hosts entry. Never disable host verification or trust a fresh
   unauthenticated scan alone. No GHCR_TOKEN secret is necessary.
6. Allow public SSH/HTTP/HTTPS only (22/80/443) for JobPing. Keep 8001/8002/8020,
   5543 and 6381 bound to loopback. Preserve shared-host rules for other services.
7. Protect the default branch: require PR review and all matrix quality checks,
   controlled browser, service acceptance and deployment failure simulation;
   disallow force pushes/deletion. Require an up-to-date branch if desired.
   Restrict the production environment to the default branch; add reviewer
   approval if organizational policy requires it. Branch protection is separate
   from the workflow's mandatory CI gate.

## Release, rollback and status

Normal operation: push branch → PR checks → merge → default-branch checks →
verified image → inactive color → migrations → readiness → proxy → workers.

```sh
sudo bash /srv/jobping/deploy/scripts/deploy.sh status
sudo docker compose -p jobping-blue -f /srv/jobping/releases/<sha>/deploy/compose.api.yml logs --tail 100
# Use metadata's IMAGE, COLOR and PORT as JOBPING_IMAGE, JOBPING_COLOR, JOBPING_PORT
# when invoking Compose directly. backend.env is not shell configuration.
sudo bash /srv/jobping/deploy/scripts/deploy.sh rollback
sudo nginx -t
curl --fail https://api.jobping.website/version
```

Status reports each color's SHA/image/time, direct readiness, proxy target, active
color and worker/scheduler version. A duplicate SHA/digest deployment is a no-op.
A reused SHA with a different digest is rejected. Manual releases require the
exact verified digest and already installed immutable release bundle:

```sh
sudo bash /srv/jobping/releases/<sha>/deploy/scripts/deploy.sh deploy <sha> sha256:<digest>
```

Authenticate root Docker to private GHCR temporarily if manually deploying
outside Actions. Do not print credentials. Routine releases do not require SSH.
The stable `/srv/jobping/deploy` link follows the last successful receiver run.
Rollback performs no pull, build or migration; it checks the previous API,
switches/validates/verifies Nginx and returns workers to the prior image. If a
failed release reused standby, the controller reconstructs that standby from its
saved immutable image/configuration, preserving rollback without rebuilding.

## Failure and recovery

Pull/label, migration, target-start or readiness failures never switch traffic.
Candidate Nginx validation uses a temporary complete config tree. Only a validated
upstream is atomically renamed into place. Reload or post-cutover failures restore
the previous healthy API and verify it through TLS/loopback Nginx. This follows
Nginx's [configuration test and graceful reload](https://nginx.org/en/docs/switches.html).
Production state is recorded only after exact-version proxy verification.
Failed-stage diagnostics include color/SHA and recent container logs, never dotenv.

A `state/pending` journal survives process death and blocks new deployments.
`status` shows it. Verify old API/database health and inspect Nginx before:

```sh
sudo bash /srv/jobping/deploy/scripts/deploy.sh recover
```

Recovery verifies/restores the journal's previous upstream and saved workers.
If automatic recovery cannot establish health, it retains the journal and leaves
potentially serving containers running. Do not delete the journal just to bypass
it. A database incompatibility needs operator repair, not another traffic switch.
Check `docker ps`, Nginx errors, `/ready`, controller output and saved image metadata.
Readiness probes SQL, Redis and its event listener with a three-second deadline;
liveness is `/health`; `/api/v1/health` remains the cheap internal Docker probe.

## Workers, migrations and resources

Workers live in a separate `jobping-workers` Compose project. After verified API
cutover the controller stops the old generation, confirms it is gone, starts the
new image and requires its Docker heartbeat health. Fixed global container names
and shared exclusive role locks defend against duplicate singleton generations.
There is a brief worker pause, with durable pending mail retained. Notification
budgets, frozen payloads, opt-ins and sending flags remain unchanged. SIGTERM
pauses polling, drains active work for up to 150 seconds, and closes resources;
Docker allows 180 seconds. A failure restores old workers without overlapping.
Manual restart uses `docker restart -t 180 jobping-scheduler` or
`docker restart -t 180 jobping-notifications`; never start another scheduler project.

Exactly one Alembic upgrade runs under the deployment lock before target startup.
Both API versions and old workers share the schema: review **expand-contract**
migrations before merging. Add nullable/defaulted structures first; deploy dual
compatible code and backfill separately; remove old structures only after all
standby/worker versions no longer require them. CI's roundtrip is not proof of
backward compatibility. No destructive migration is added by this feature.
Migration failure aborts traffic switch; an API rollback never downgrades schema
or restores database backups over newer activity. Back up before schema releases.

The VM currently has 24 GB RAM and ample disk; both APIs are small compared with
shared infrastructure. Logs remain bounded. Cleanup removes only labeled JobPing
image references without any container using them; it never removes volumes or
images required by active/standby/workers. Old release directories remain for
review/recovery; prune only unreferenced ones deliberately. Existing 14-day daily
database backups remain in place; off-server recovery is tracked in DEPLOY-002.

## Validation

`pytest tests/deployment` on Linux runs the actual Bash controller with isolated
Docker/Nginx/smoke fixtures: both colors, exact digests, malformed/missing state,
no-op, timeout, migration/startup failure, proxy validation/reload failure,
post-switch restore, unhealthy rollback, lock collision and worker ordering.
The full simulation is v1 blue → v2 green → broken v3 → restored v1 standby →
rollback to blue. It needs no production VM or network. CI additionally executes
the real production image against disposable PostgreSQL/Redis and all existing
browser/service/migration checks. Windows skips Linux-controller tests; probe
and scheduler regressions run on every supported platform.
