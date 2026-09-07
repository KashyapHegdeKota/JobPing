# Email notifications

JobPing sends opted-in users individual matching-job alerts and one daily recap at
8 PM in their saved IANA timezone. The recap includes all matching discoveries in
its window, even when an individual alert was sent. Empty recaps are skipped.
The backend runs on your VM; Render is not required.

## Setup

1. Install dependencies with `poetry install` and apply `poetry run alembic upgrade head`
   against the production PostgreSQL database during your deployment window.
   Migration `0004_notifications` creates only notification tables. Existing jobs
   are not backfilled. Both single and bulk ingestion create durable events in their
   own SQL transaction, regardless of Redis publisher configuration.
2. Own and verify a sending domain in Resend. For `no-reply@jobping.com`, verify
   `jobping.com` and add the DNS records Resend supplies (SPF/DKIM; configure DMARC
   according to your domain policy). A mailbox or your own SMTP service is not
   required. The worker calls `https://api.resend.com/emails` using HTTPS.
3. Set `RESEND_API_KEY` to a sending-only key, preferably restricted to the verified
   domain, and `RESEND_FROM` to the bare sender address. Keep open/click tracking
   disabled in Resend. Set `NOTIFICATION_APP_URL` to the public UI origin and
   `NOTIFICATION_API_URL` to the public FastAPI origin. Use HTTPS in production.
4. Set `FIREBASE_PROJECT_ID` to the UI's Firebase project and provide a Firebase
   Admin service account through `GOOGLE_APPLICATION_CREDENTIALS`, outside Git.
   The API validates Firebase tokens and revocation; the worker verifies the current
   email, verification state, and enabled account again before every send.
5. Generate a Fernet key with
   `poetry run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   Store it in a secret environment file as
   `NOTIFICATION_ENCRYPTION_KEYS={"v1":"<generated key>"}` with
   `NOTIFICATION_KEY_VERSION=v1`. This is needed for BYOK, not shared sending.
   Keep the encryption key separately from database backups and restrict access to
   the VM service user. API keys and webhook secrets are never returned to clients.
6. Set `NEXT_PUBLIC_API_URL` in the UI to the backend origin and allow that UI origin
   in backend `CORS_ORIGINS`. Only the Firebase public configuration and API origin
   belong in the UI; never expose Resend keys or encryption keys there.
7. Configure a Resend webhook for `email.delivered`, `email.bounced`, and
   `email.complained` at
   `https://YOUR_API/api/v1/notifications/webhooks/shared`. Put its signing secret in
   `RESEND_WEBHOOK_SECRET`. Requests are signature/timestamp checked and deduplicated.
   Hard bounce/complaint events suppress the recipient and cancel pending emails.
8. Start with `NOTIFICATIONS_SEND_ENABLED=false`. Opt in one verified test account
   from `/profile`, ingest a controlled new listing, and run
   `poetry run python -m app.cli notifications-worker --once`. Inspect the queue and
   synthetic previews before enabling sending. Dry-run processes events and creates
   durable delivery records but never calls Resend. Production URL/credentials must
   be set in the API and worker environments consistently.
9. Enable `NOTIFICATIONS_SEND_ENABLED=true` in both environments and restart the
   worker/API. Check one controlled inbox, then expand to the ten-user MVP. No live
   email is sent by tests or migrations.

If a database has the former `0003_application_checkpoint_stage` revision stamped,
verify that all three checkpoint columns already exist, then use Alembic's stamp
operation to set `0003_checkpoint_stage` before upgrading. Do not rerun the checkpoint
DDL over existing columns. New installations use the corrected revision directly.

## VM service

Create a project-local `.venv` on the VM and install the app there. The example
`deploy/jobping-notifications.service` assumes `/opt/jobping`, a `jobping` service
user, and `/etc/jobping/notifications.env`. Adapt those paths to your VM, install the
unit, then enable/start it with systemd. Keep the API, scraper and notification worker
as separate services; the notification worker does not depend on Redis availability.

The worker registers a non-overlapping 15-second poll. It sends at most 20 emails
per tick with a one-second pause between API calls, and owns all clients and engines.
Database serialization and persisted leases prevent concurrent workers from claiming
the same delivery. A process crash leaves a two-minute lease that can be reclaimed.
Graceful shutdown/cancellation preserves that recovery path.

Bootstrap imports must explicitly set `NOTIFICATIONS_SUPPRESS_DISCOVERY=true` in
the scraper process, then remove it for normal polling. The repository also accepts
`suppress_notifications=True` for programmatic bootstrap calls. Sending disabled is
different: it still records discoveries and queues mail for later review/delivery.

## Preferences and free-tier budget

- A verified Firebase email and explicit opt-in are required. Both categories start
  disabled. Job types and seasons are saved independently of feed filters.
- Changes apply to discoveries after the settings change. Already committed events
  are matched under the old preferences before changing them. Disabling a category
  cancels its pending mail; an already dispatched request cannot be recalled.
- The first ten active shared-provider subscribers are admitted atomically. Beyond
  that, users can configure BYOK. Removing BYOK disables both opt-ins; a settings save
  explicitly requests a shared place again.
- Resend Free provides 100 messages/day and 3,000/month, counted per recipient.
  JobPing intentionally uses stricter local limits: 90 per rolling 24 hours and
  2,900 per rolling 31 days. Remaining slots are reserved for recaps: one per active
  recap subscriber daily and 31 per subscriber in the longer window. This conservative
  accounting avoids assumptions about provider reset times and leaves safety margin.
- Due recaps get priority. Individual alerts rotate by recipient's latest charged
  delivery. Over-budget alerts remain available in the recap; unsent individual
  messages are folded into the recap at its cutoff, or expire after 24 hours.
- Pending recaps that have never been attempted are combined at the next cutoff
  into one catch-up email. Worker downtime similarly creates one recap spanning
  missed windows. Quota availability and provider failures can delay delivery.
- Every attempted send is conservatively counted locally, including uncertain
  failures. Provider usage headers and explicit quota errors also pause the account;
  external activity in the same Resend account cannot be fully reserved by JobPing.

## BYOK

In Profile, users provide a sending-only Resend key and their own verified From
address. Their Resend account cannot automatically send from JobPing's domain.
Clicking **Send test email** enqueues one message to their own verified Firebase
email, charged to their own allowance. Acceptance activates the connection. Test
requests are limited to one per five minutes and share the connection's send budget.

No arbitrary recipient, template, endpoint, or URL can be supplied to the sending API.
JobPing always sends its own templates to the account owner. BYOK uses independent
local quota accounting; an invalid key or sender pauses that connection without
falling back to JobPing's shared key. Replacing credentials requires a new test and
cancels pending mail associated with the previous configuration. New discoveries
continue to be recorded while a connection awaits its test.

The Profile page supplies a unique webhook URL. Add it to the user's Resend account
for the same three event types, then save the connection with the webhook signing
secret. Without that secret, the UI reports provider acceptance rather than confirmed
delivery; Resend's own suppression remains in effect. A replacement connection form
requires the API key again; stored secrets are not prefilled.

To rotate encryption keys, add `v2` alongside `v1` in the JSON key map and set
`NOTIFICATION_KEY_VERSION=v2`. New connections use v2; existing v1 ciphertext remains
readable. Replace saved connections (or re-encrypt them with an operator-controlled
maintenance script) before removing v1. Never remove a version while rows still use it.

## Delivery recovery and operations

Each delivery freezes its payload and uses its database ID as the Resend idempotency
key. Retries keep that payload unchanged. Transient failures back off; definite sender
errors pause sending. Resend remembers idempotency keys for 24 hours; JobPing stops
automatic retries at 23 hours and marks unresolved outcomes `reconcile`. Review the
Resend dashboard by delivery/idempotency ID before any manual resend. Do not reset
uncertain records to pending after the provider's idempotency window expires.

`accepted` means Resend accepted the API request, not guaranteed inbox delivery.
Verified webhooks promote it to `delivered` or mark bounce/complaint outcomes. A
webhook racing an in-flight response receives 503 so Resend can retry it.

The worker logs `notification.heartbeat` (pending count and oldest age) and
`notification.send` (delivery ID and result code), without recipients, bodies or keys.
Alert on a missing heartbeat for five minutes, growing pending age, `reconcile`,
`sender_error`, or repeated quota pauses. Example database inspection:

```sql
SELECT status, count(*) FROM notification_deliveries GROUP BY status;
SELECT id, kind, status, error_code, created_at, due_at
FROM notification_deliveries
WHERE status IN ('pending', 'reconcile', 'failed')
ORDER BY created_at;
```

Use these IDs to investigate in the provider dashboard. Fix configuration or replace
a broken BYOK connection from Profile. A suppressed email cannot opt in again through
the API; an operator must investigate the bounce/complaint before lifting suppression.
Retain delivery timestamps for at least 31 days so quota accounting stays accurate.
The MVP has no automatic retention purge. Protect database backups because delivery
payloads contain recipient addresses and notification content.

## Templates and verification

`poetry run python -m app.cli notifications-preview` writes synthetic alert and recap
HTML to `.pytest_cache/email-preview`. It uses no credentials or database. The shared
Jinja2 template escapes job content, uses inline CSS and presentation tables, and
ships plain-text alternatives. Links are restricted to HTTP(S). Large recaps stay
under approximately 90 KB and link to an authenticated page with every matching job.

Run `poetry run pytest`, `poetry run ruff check .`, and `poetry run black --check .`.
If Windows' shared pytest temp directory is inaccessible, use a fresh directory under
`.pytest_cache` with `--basetemp`. If sibling `.worktrees` contain unrelated formatting
issues, scope Black to this checkout's `app tests alembic` directories. UI validation
uses `npm run lint`, `npm run test`, and `npm run build`.

Tests use isolated SQLite databases, fake Firebase identities and mocked Resend HTTP
responses. Before launch, perform a real Gmail/Outlook inbox check with the verified
production sender; local browser previews do not substitute for email-client QA.

References: [Resend domains](https://resend.com/docs/dashboard/domains/introduction),
[Resend quotas](https://resend.com/docs/knowledge-base/account-quotas-and-limits),
[idempotency](https://resend.com/docs/dashboard/emails/idempotency-keys),
[Firebase token verification](https://firebase.google.com/docs/auth/admin/verify-id-tokens).
