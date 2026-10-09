# Oracle VM production deployment

The backend runs at `https://api.jobping.website` on the Oracle Ubuntu ARM64 VM
`137.131.24.56`, alongside its existing applications. The Vercel frontend uses
`NEXT_PUBLIC_API_URL=https://api.jobping.website` in Production and Preview.
Only the production frontend origins are allowed by CORS; add any deliberate
preview origin separately before using it to access authenticated API features.

Connect through WSL:

```powershell
wsl -d Ubuntu -- bash -lc 'ssh -i ~/.ssh/oracle_key ubuntu@137.131.24.56'
```

The application source is `/opt/jobping`. Root-only configuration is
`/etc/jobping/backend.env`; Firebase credentials are mounted read-only from
`/etc/jobping/firebase`. Credentials, encryption keys, candidate files and the
development database are excluded from the source archive and Docker image.
The production database has independently generated credentials and named volumes.

`deploy/compose.production.yml` runs API, scheduler, PostgreSQL 16 and Redis 7 with
automatic restart and bounded container logs. This VM's Docker bridge routing is
incomplete, so JobPing uses host networking without modifying shared firewall
rules or restarting Docker. PostgreSQL binds `127.0.0.1:5543`, Redis binds
`127.0.0.1:6381`, and Uvicorn binds `127.0.0.1:8020`. Never change these to public
interfaces. Nginx is the public boundary on ports 80/443 and supports WebSocket
upgrades and unbuffered SSE. Certbot manages the certificate and automatic renewal.
Run one API worker: its event connection manager is process-local.

Operations on the VM:

```sh
cd /opt/jobping
sudo docker compose -f deploy/compose.production.yml ps
sudo docker compose -f deploy/compose.production.yml logs --tail=100 api scheduler
sudo docker compose -f deploy/compose.production.yml build api
# Back up before deploying code that requires schema changes.
sudo systemctl start jobping-backup.service
sudo docker compose -f deploy/compose.production.yml run --rm api alembic upgrade head
sudo docker compose -f deploy/compose.production.yml up -d api scheduler
curl --fail https://api.jobping.website/api/v1/stats
sudo nginx -t
```

Upload reviewed source before rebuilding; the deployed archive records backend
revision `5d5fa0b`; the launch image is also tagged
`jobping-backend:5d5fa0b-launch-20261009`. Uncommitted local example/secret changes were excluded.
Keep a tagged previous image before future builds for code rollback. Schema
rollback needs a reviewed migration plan or a database restore; do not blindly
restore a backup over later user activity. Never use Compose `down -v` in production.

The October 9 publication-date correction is deployed as
`jobping-backend:dates-20261009`, with the previous code retained as
`jobping-backend:pre-date-fix-20261009`. This adds reviewed changes to
`app/scrapers/greenhouse.py` and `app/services/posting_dates.py` over the launch
archive. Repolling repaired missing Waymo publication dates with notification
suppression and preserved existing discovery history. The matching UI revision
is `25e545e`; both fixes must be retained on future source deployments.

The October 9 email-conversation rollout deploys merged backend revision `fa06020`
as `jobping-backend:threading-fa06020-20261009`, also tagged as the running `local`
image. API, scheduler and notifications were recreated without restarting the
database/cache or changing configuration. No migration was required. A fresh verified
database dump and `/var/backups/jobping/source-pre-threading-20261009.tar` were retained;
the previous runtime image is `jobping-backend:pre-threading-20261009`.

Read-only preflight and deployed-container checks rendered two real job occurrences
with distinct bodies and a common alert subject/reference. Existing frozen deliveries
retain their headers; only newly rendered alerts use conversation grouping. Public
HTTPS/CORS/auth/WebSocket/SSE checks and worker/direct-ATS polling pass. No test email
or discovery event was created by verification. Confirm actual Gmail conversation
grouping on the next matching alerts with conversation view enabled.

For code rollback, retag the previous image as `jobping-backend:local` and recreate
only API/scheduler/notifications with Compose `--no-deps`. Restore the reviewed source
archive before another build. This release has no schema rollback; preserve current
database activity and private configuration.

The initial Simplify main/off-season, ApplyGuy and direct ATS syncs used
`NOTIFICATIONS_SUPPRESS_DISCOVERY=true`. Normal scheduler polling records new
events (`NOTIFICATIONS_SUPPRESS_DISCOVERY=false`). Email delivery was enabled on
October 9 after a controlled, Firebase-verified test produced exactly one send,
a signed delivered callback and user-confirmed Gmail inbox receipt. Resend's
verified sender is `no-reply@jobping.website`; open/click tracking is disabled.
The delivered/bounced/complained webhook now points to
`https://api.jobping.website/api/v1/notifications/webhooks/shared`, replacing the
development tunnel, with its existing signing secret retained.

`NOTIFICATIONS_SEND_ENABLED=true` applies to API and worker through the shared
root-only environment file. The `notifications` Compose profile is running with
automatic restart and a 15-second poll. Only verified, opted-in accounts receive
recurring mail; the test inbox was not enrolled automatically. Saved timezone
recaps run at 8 PM, with the existing budget and suppression rules intact.

```sh
sudo docker compose -f deploy/compose.production.yml --profile notifications ps
sudo docker compose -f deploy/compose.production.yml logs --tail=100 notifications
# To pause delivery, set NOTIFICATIONS_SEND_ENABLED=false in the private env file,
# then recreate API and notifications and stop the notifications service.
sudo docker compose -f deploy/compose.production.yml --profile notifications up -d api notifications
sudo docker compose -f deploy/compose.production.yml stop notifications
```

The previous private configuration is retained as root-only
`/etc/jobping/backend.env.before-email-20261009`. Do not blindly restore this over
later configuration changes; edit only the sending flag to pause mail. Follow
`docs/notifications.md` for budgets, verified-recipient checks and delivery recovery.

`jobping-backup.timer` takes a daily PostgreSQL custom-format dump at approximately
09:00 UTC, verifies its archive listing, and retains 14 days in root-only
`/var/backups/jobping`. Encryption keys are outside database dumps. These backups
are on the same VM: copy them to separately protected off-server storage for
disaster recovery. Inspect with `sudo systemctl list-timers jobping-backup.timer`.
The Redis append-only volume persists cache state; PostgreSQL remains the source
of truth. A fresh Redis cache does not erase job or occurrence history.
