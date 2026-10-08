# JobPing

## Analytics

Signed-in users can view their own activity, filter usage, matching occurrences and
email counts at `/activity`. Admins get aggregate site totals at `/admin/analytics`:
active signed-in users over 24 hours/7 days/30 days, unique jobs, open jobs, reposts,
subscribers and email delivery counts. Set server-side `ANALYTICS_ADMIN_UIDS` to a
comma-separated list of verified Firebase user IDs, then restart the API. An empty
list denies admin access. No admin secrets or allowlist belong in the UI.

Apply `poetry run alembic upgrade head` (`0008_analytics`) before using the feature.
Activity starts when the updated UI is deployed; existing job/email totals come from
the durable database. Counts cover signed-in activity, not anonymous visitors or all
accounts registered in Firebase. Daily activity uses UTC. “Sent” counts unique
deliveries with provider acceptance IDs, not retries; confirmed delivery relies on
webhooks. Clicking a job link does not prove submission.

Tracking records allowed page names, category/remote/date/company filter settings,
whether search was used, and job IDs. Raw queries, URL query strings, email bodies,
tokens, IP addresses and application answers are not stored. The personal API is
scoped to the server-verified account; the site API contains aggregates only.
Raw activity is retained until an operator deletes it; no third-party analytics
service is involved. Recording failures never block browsing.

## Email delivery setup

Opt-in job alerts, 8 PM daily recaps, and optional encrypted Resend BYOK settings
are available from the UI Profile page. Run `poetry run python -m app.cli notifications-worker`
on the VM; sending defaults to disabled. See [notification setup and operations](docs/notifications.md)
for domain verification, Firebase credentials, migrations, quotas and systemd setup.
Generate offline HTML samples with `poetry run python -m app.cli notifications-preview`.

## Application Inspection

JobPing can inspect supported ATS application forms without modifying or submitting them:

```text
poetry run python -m app.cli inspect-application (job-id)
```

Currently supported: Greenhouse inspection. Lever and Workday inspection are planned.
The `private/` directory is intentionally gitignored; use the sanitized files in
`examples/` as a starting point for local candidate configuration.

## Autonomous application backend (Phase 2)

The application backend stores one durable checkpoint per job in
`application_attempts` (including its current browser URL and coarse checkpoint stage)
and non-secret answer audit records in `application_answers`. Apply the
`0002_application_agent` and `0003_checkpoint_stage` Alembic migrations
after the initial schema. The
candidate profile loader validates `private/candidate.json` and its resume path; use
the sanitized candidate, answer, story, and rule files in `examples/` as templates.

The Codex application agent uses the high-level JobPing MCP facade for job facts,
candidate facts, deterministic answer lookup, and application state. Browser
perception and interaction remain in the Codex Chrome extension. JobPing does not
fill forms, retrieve OTPs, bypass CAPTCHA, or store authentication secrets.

Useful operational commands:

```text
poetry run python -m app.cli applications queue
poetry run python -m app.cli applications ready
poetry run python -m app.cli applications review
poetry run python -m app.cli applications verification
poetry run python -m app.cli applications status <job-id>
poetry run python -m app.cli applications reset <job-id> --confirm
```

`config.example.yaml` sets `applications.auto_submit: false`; the safe initial
workflow stops at `READY_TO_SUBMIT` for a human review and submission.

The server registers high-level `jobs_*`, narrow `candidate_*`, `stories_get`, and
`application_*` business tools and owns one SQLAlchemy engine lifecycle. Set
`JOBPING_CANDIDATE_PATH`, `JOBPING_ANSWERS_PATH`, `JOBPING_STORIES_PATH`, and
`JOBPING_RULES_PATH` to override the default files under `private/`.

In PowerShell, first point `DATABASE_URL` at the same already-migrated PostgreSQL
database used by JobPing. Do not use a fresh SQLite database here: it will not contain
your real queued jobs. Run the migration before registering the server. Keep database
credentials in your environment or local secret manager, never in Git:

```powershell
$repo = (Resolve-Path .).Path
if (-not $env:DATABASE_URL) { throw "Set DATABASE_URL to the migrated JobPing PostgreSQL database first" }
$env:JOBPING_CANDIDATE_PATH = (Join-Path $repo "private\candidate.json")
$env:JOBPING_ANSWERS_PATH = (Join-Path $repo "private\answers.json")
$env:JOBPING_STORIES_PATH = (Join-Path $repo "private\stories.json")
$env:JOBPING_RULES_PATH = (Join-Path $repo "private\application_rules.json")

poetry -C $repo run alembic upgrade head

codex mcp add jobping `
  --env "DATABASE_URL=$env:DATABASE_URL" `
  --env "JOBPING_CANDIDATE_PATH=$env:JOBPING_CANDIDATE_PATH" `
  --env "JOBPING_ANSWERS_PATH=$env:JOBPING_ANSWERS_PATH" `
  --env "JOBPING_STORIES_PATH=$env:JOBPING_STORIES_PATH" `
  --env "JOBPING_RULES_PATH=$env:JOBPING_RULES_PATH" `
  -- poetry -C $repo run python -m app.mcp.server

codex mcp list
```

To prepare one real application, ask Codex:

```text
Prepare application <JOB_ID> using JobPing. Use the Chrome browser. Stop at READY_TO_SUBMIT.
```

After a user completes a verification checkpoint, ask:

```text
resume application <JOB_ID>
```

Submit only with this explicit instruction after reviewing the ready application:

```text
submit application <JOB_ID>
```

JobPing never fills forms, reads OTPs, bypasses CAPTCHA, stores credentials, or submits
unattended.

JobPing is a low-latency job discovery engine for 2026/2027 technology internships and
new-grad roles. The ingestion foundation includes GitHub commit retrieval, unified-diff and
Markdown parsing, direct ATS scrapers (Greenhouse, Lever, Workday, Custom Tech), network interception, browser automation, normalization, dual SHA-256 hashing, Redis-backed state classification, and SQLAlchemy persistence with Alembic migrations.

The CLI supports fetching and classifying Simplify commits and ApplyGuy's machine-readable
JSON feeds (with database persistence), running a persistent scheduler daemon, and auditing
the database for anomalies.

## Prerequisites

- Python 3.12 or newer (but earlier than Python 4)
- [Poetry 2](https://python-poetry.org/docs/#installation)
- Docker Desktop or Docker Engine with the Compose plugin
- Git

## Setup

Clone the repository, enter its root directory, install the locked dependencies, and create a
local environment file:

```shell
git clone https://github.com/KashyapHegdeKota/JobPing.git
cd JobPing
poetry install --with dev
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```shell
cp .env.example .env
```

Docker Compose reads `.env` automatically. Python and Alembic do not load that file by
themselves; export the relevant variables in your shell before invoking them. The checked-in
values are local-development defaults only. Change the PostgreSQL password for any shared or
deployed environment.

## Environment variables

| Variable | Default/example | Used by | Purpose |
| --- | --- | --- | --- |
| `POSTGRES_DB` | `jobping` | Docker Compose | Database created by the PostgreSQL container. |
| `POSTGRES_USER` | `jobping` | Docker Compose | PostgreSQL role created by the container. |
| `POSTGRES_PASSWORD` | local placeholder | Docker Compose | Password for `POSTGRES_USER`; keep real secrets out of Git. |
| `DATABASE_URL` | `postgresql+psycopg://...@localhost:5432/jobping` | Alembic | Synchronous SQLAlchemy URL used for migrations. If unset, Alembic falls back to `sqlite:///./jobping.db`. |
| `REDIS_URL` | `redis://localhost:6379/0` | CLI/deduplicator | Redis connection and logical database used for deduplication state. |
| `GITHUB_TOKEN` | unset | CLI/GitHub client | Optional bearer token. Recommended to increase GitHub API limits; never commit it. |
| `GITHUB_OWNER` | `SimplifyJobs` | CLI | Target repository owner. |
| `GITHUB_REPO` | `Summer2027-Internships` | incremental CLI | Target repository name. |
| `SIMPLIFY_REPOSITORIES` | `Summer2027-Internships,New-Grad-Positions` | full-sync CLI | Current-cycle repositories fetched from `dev`. |
| `GITHUB_REF` | `HEAD` | CLI | Commit SHA, tag, or branch to process. |
| `SIMPLIFY_FULL_SYNC_REF` | `dev` | full-sync CLI | Simplify's live-updates branch used for raw-file bootstrap reads; intentionally separate from GitHub Actions' reserved `GITHUB_REF`. |
| `TARGET_README` | `README.md` | CLI | Exact Markdown path inspected in the commit. |
| `TARGET_READMES` | `README.md,README-Off-Season.md` | full-sync CLI | Comma-separated raw Markdown paths used when no repeatable `--target-readme` options are provided. |
| `JOB_SEASON` | `2026` | CLI | Hiring season; accepted values are 2026 and 2027. |
| `JOB_TYPE` | `internship` | CLI | Assigned category; accepted values are `internship` and `new_grad`. |

Command-line options override their corresponding CLI environment variables.

ApplyGuy ingestion

ApplyGuy's 2027 repositories are supported through their machine-readable JSON files:

* `ApplyGuy/2027-Internships/data/internships.json`
* `ApplyGuy/2027-New-Grad-Jobs/data/new-grad-jobs.json`

Run one feed or both feeds manually (the command requires `DATABASE_URL`):

```shell
poetry run python -m app.cli run-applyguy-sync --type all
poetry run python -m app.cli run-applyguy-sync --type internship
poetry run python -m app.cli run-applyguy-sync --type new-grad
```

ApplyGuy's `listingUrl` is preferred over its `url` redirect. Job identity remains global:
company/title hashing and the unique `base_hash` prevent a role already discovered through
Simplify, Greenhouse, Lever, Workday, or another source from becoming a second posting.
Tracking parameters are removed from application URLs, and direct ATS/employer URLs are
retained over aggregator redirects. The scheduler polls the internship and new-grad feeds
independently under the `applyguy.ai` interval, so one feed failure does not stop the other.
Temporary disappearance from ApplyGuy is not treated as closure; only explicit closed state
from a source can close a posting.

Direct Greenhouse and Lever polling

Both providers have real scheduler callbacks and a one-shot `run-ats-sync` command.
They require a local JSON board registry; no configured boards means that provider
is disabled. Copy `examples/ats_sources.example.json` to `private/ats_sources.json`
and replace the placeholder tokens and company names with your chosen boards.
Use the public board/site token, not a URL, and use the same company name used by
Simplify/ApplyGuy so global company/title identity stays consistent.

Each entry requires `provider` (`greenhouse` or `lever`), `token`, `company`, and
`season` (2026 or 2027). Optional `job_types` defaults to both `internship` and
`new_grad`; optional `allow_undated` defaults to false. Unknown fields, duplicate
boards, empty categories, and invalid tokens are rejected before any network work.

Eligibility uses titles only: explicit intern/internship/co-op or new/recent/
university/college graduate labels and graduate engineer/developer/analyst/program/
trainee labels. Senior/staff/principal/director/head/lead titles, generic entry-level
roles, ambiguous mixed categories, and titles with other years are excluded.
By default a title must explicitly contain the configured year. Set
`allow_undated: true` on a board only if you intend eligible titles without a year
to belong to its configured season. Description years and posting dates do not
establish a season. This conservative policy can miss eligible roles; it avoids
classifying entire boards as internships or new-grad jobs.

PowerShell:

```powershell
$env:ATS_SOURCES_FILE = "private/ats_sources.json"
poetry run python -m app.cli run-ats-sync --dry-run
poetry run python -m app.cli start-scheduler --dry-run
# Initial seed: suppress historical discovery notifications.
$env:NOTIFICATIONS_SUPPRESS_DISCOVERY = "true"
poetry run python -m app.cli run-ats-sync
$env:NOTIFICATIONS_SUPPRESS_DISCOVERY = "false"
poetry run python -m app.cli start-scheduler
```

Both commands also accept `--sources-file PATH`. A real sync requires
`DATABASE_URL` and Redis; its dry-run only validates configuration. Scheduler
dry-run validates configured ATS database requirements and lists active boards.
Polling intervals are `boards.greenhouse.io` and `api.lever.co` (120 seconds by
default), with independent targets per board. Summaries show fetched, filtered,
accepted and failed counts; one-shot sync exits nonzero on a board failure.
Public board responses describe active jobs only; disappearance never closes a job.
These JSON API clients do not require browser automation. Lever currently uses
the existing global `api.lever.co` endpoint; EU-hosted Lever boards are unsupported.

The requested technology-company preset is `examples/ats_sources.tech_companies.json`.
See [selected company coverage](docs/direct-ats-company-coverage.md) for verified
boards, strict-filter counts, subsidiaries and remaining portal integrations.

Location reconciliation

Each posting stores the source that supplied its current meaningful location. Greenhouse,
Lever, Workday, Amazon, and Meta observations have the highest authority, Simplify is next,
and ApplyGuy is lower. A source may correct its own previous value; when different recognized
sources at the same tier disagree, a stable source-name tie-break prevents polling order from
causing location ping-pong. Unknown sources do not implicitly outrank recognized sources,
and empty or `Unspecified` locations cannot erase a meaningful location. Distinct cities are
preserved, and no fuzzy location matching is used. The effective location is selected before
Redis classification during database-backed ingestion so its content hash agrees with the
persisted posting. Run `poetry run alembic upgrade head` before using the updated ingestion
code. Standalone runs without a database have no persisted provenance to reconcile against.

## Start PostgreSQL and Redis

After creating `.env`, start both services in the background:

```shell
docker compose up -d
docker compose ps
```

`docker compose ps` should report both `postgres` and `redis` as healthy. Direct health checks
are also available:

```shell
docker compose exec postgres pg_isready -U jobping -d jobping
docker compose exec redis redis-cli ping
```

The services expose PostgreSQL 16 on `localhost:5432` and Redis 7 on `localhost:6379`.
Named volumes preserve their data. Stop containers with `docker compose down`; adding `-v`
also deletes the local database and Redis volumes.

## Database migrations

Export the PostgreSQL URL from `.env`, then apply or inspect migrations:

PowerShell:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://jobping:change-me-for-local-development@localhost:5432/jobping"
poetry run alembic upgrade head
poetry run alembic current
poetry run alembic check
```

macOS/Linux:

```shell
export DATABASE_URL='postgresql+psycopg://jobping:change-me-for-local-development@localhost:5432/jobping'
poetry run alembic upgrade head
poetry run alembic current
poetry run alembic check
```

To inspect SQL without connecting to the database, run
`poetry run alembic upgrade head --sql`. If `DATABASE_URL` is omitted, online migration
commands operate on the local `jobping.db` SQLite fallback instead of the Docker database.

## Run the Simplify parser

Redis must be healthy. Process the default repository and `HEAD` commit:

```shell
poetry run python -m app.cli run-simplify-parser
```

Provide options explicitly when needed (include `--database-url` to persist parsed results):

```shell
poetry run python -m app.cli run-simplify-parser \
  --owner SimplifyJobs \
  --repo Summer2026-Internships \
  --ref HEAD \
  --target-readme README.md \
  --season 2026 \
  --job-type internship \
  --redis-url redis://localhost:6379/0 \
  --database-url postgresql+psycopg://jobping:change-me-for-local-development@localhost:5432/jobping
```

Use `--help` for the authoritative option list. The command prints counts for `NEW_ROLE`,
`ROLE_UPDATED`, `ROLE_CLOSED`, `NO_OP`, and rejected rows. Redis state has a 90-day TTL, so
reprocessing an unchanged role normally returns `NO_OP`.

Bootstrap PostgreSQL directly from the current raw files on Simplify's live `dev` branch:

```shell
poetry run python -m app.cli run-simplify-full-sync \
  --repo Summer2027-Internships \
  --repo New-Grad-Positions \
  --target-readme README.md \
  --target-readme README-Off-Season.md \
  --database-url postgresql+psycopg://jobping:change-me-for-local-development@localhost:5432/jobping
```

This command bypasses commit patches, parses each complete file, and submits all unique jobs
to one bulk-upsert persistence operation. It prints a per-file classification summary and the
final parsed/persisted batch size. Omit a `--target-readme` value when that file does not exist
in the selected repository.

GitHub rejects exhausted unauthenticated requests with HTTP 403 or 429. Set `GITHUB_TOKEN`
to a GitHub token when polling regularly; the client also reports rate-limit reset information
when GitHub supplies it. The token option is intentionally hidden from CLI help—prefer the
environment variable so it does not appear in shell history.

## Start the Scheduler Daemon

The asynchronous polling scheduler can be started to repeatedly execute extraction logic across multiple domains:

```shell
poetry run python -m app.cli start-scheduler
```

You can customize the polling intervals:

```shell
poetry run python -m app.cli start-scheduler --interval github.com=60 --interval boards.greenhouse.io=120
```

## Audit Database

You can audit the integrity of the persisted data without modifying data using:

```shell
poetry run python -m app.cli audit-db
```

## Tests and quality checks

The suite is deterministic and does not require live GitHub, Redis, or PostgreSQL services:

```shell
poetry run pytest
poetry run ruff check .
poetry run black --check .
```

Apply automatic formatting and safe lint fixes with:

```shell
poetry run ruff check --fix .
poetry run black .
```

## System Architecture

```text
Extractors (Simplify GitHub, Greenhouse, Lever, Workday, Browser Automation)
  -> Scraper Engine (Rate Limiting, User-Agent Rotation, Exponential Backoff)
  -> RawJobPayload
  -> Pipeline Orchestrator
  -> Pydantic Normalization (NormalizedJob)
  -> Dual SHA-256 Hashing (Base Identity + Content State)
  -> Atomic Redis Classification (NEW_ROLE | ROLE_UPDATED | ROLE_CLOSED | NO_OP)
  -> Async SQLAlchemy Repository
  -> PostgreSQL (companies, job_postings, status_logs)
```

Key paths:

- `app/scrapers/`: GitHub client and patch/Markdown parsers
- `app/pipelines/`: Simplify ingestion orchestration
- `app/services/`: hashing and Redis deduplication
- `app/db/`: SQLAlchemy models and repository operations
- `app/schemas/`: raw and normalized Pydantic models
- `alembic/`: schema migrations
- `tests/`: unit and integration coverage

## Customized BYOK tracker agents

Use `trackers create` to ask an AI agent to design a tracker for a specific posting
or a company's careers page. Describe your filters and the facts you want watched,
such as location, salary, requirements, or application deadlines. The agent creates
a validated plan and initial snapshot; subsequent checks report added, updated,
and missing matching listings with before/after facts.

PowerShell example (replace the URL and model with your own):

```powershell
# Read the key without putting its value into shell command history.
$env:JOBPING_TRACKER_API_KEY = Read-Host "Provider API key" -MaskInput
poetry run python -m app.cli trackers create "https://example.com/careers" --scope company --request "Watch remote engineering internships; track location, salary and deadline" --model YOUR_MODEL --interval 3600
poetry run python -m app.cli trackers list
poetry run python -m app.cli trackers show TRACKER_ID
poetry run python -m app.cli trackers run TRACKER_ID
poetry run python -m app.cli trackers run TRACKER_ID --watch --max-checks 24
```

Use `--scope posting` (the default) for an individual job URL. Copy `id` from the
creation JSON or `trackers list`. `--watch` runs in the foreground until Ctrl+C;
`--max-checks` limits polling attempts, including failures (0 means unlimited).
Each tracker has its own persisted interval, with a minimum of 60 seconds.

BYOK uses an HTTPS OpenAI-compatible `/chat/completions` endpoint. Set
`--base-url https://your-provider.example/v1` and `--key-env YOUR_KEY_VARIABLE`
for another provider. The provider/model must support JSON object responses and
`max_completion_tokens` as described in the
[Chat Completions API](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create).
The model is required (`--model` or `JOBPING_TRACKER_MODEL`); no model or paid
account is selected automatically. Keys are read from the process environment,
never saved in tracker settings or sent to the job site. `.env` is not loaded by
these commands. Creation makes two model requests; each polling attempt makes at
most one. The request, tracking plan, and fetched page text go to your chosen
provider and use that provider's billing and data policies.

Plans and the latest 100 successful snapshots/change sets live in gitignored
`private/trackers/`. Use `--store PATH` on any command, or `JOBPING_TRACKER_DIR`,
to change the location. No PostgreSQL, Redis, migrations, or application submission
are involved. Checks hold an exclusive tracker lock and replace the JSON file
atomically. If a process is forcibly killed, remove its `<id>.lock` file only
after verifying no check for that tracker is running.

Current scope is a single public, server-rendered HTML page per tracker, with
links resolved relative to its final URL. There is no company-name search,
pagination/crawling, JavaScript rendering, login, or CAPTCHA handling. Supply a
narrower page when the page exceeds 1 MB or 60,000 extracted characters. The
agent rejects incomplete extractions, unknown job links, duplicate results, and
invalid model responses; failures preserve the last good snapshot. Watch mode
retries extraction/transport failures at the next interval. An HTTP 404 is an
error, not proof of closure. A disappeared listing is `missing`; only explicit
page evidence should produce `closed`. AI completeness and fact extraction can
still be mistaken; inspect the saved plan and facts. The first successful check
is a baseline with no change alerts. Alerts are CLI JSON and local history only.

Tracker tests mock all source and provider traffic, including a complete CLI
create/check/persist flow; they require no key or paid network calls:

```text
poetry run pytest tests/unit/test_trackers.py
```

## Troubleshooting

See [automated testing and release evidence](docs/testing.md) for the application
acceptance suites, controlled Chromium fixtures, isolated PostgreSQL/Redis tests,
CI matrix, coverage reports, and remaining browser-agent release gates.

- **Compose reports blank PostgreSQL variables:** create `.env` from `.env.example` before
  running `docker compose up`.
- **Port 5432 or 6379 is already allocated:** stop the conflicting local service or change the
  host-side port and update the corresponding connection URL.
- **A container is unhealthy:** inspect `docker compose ps` and
  `docker compose logs postgres redis`; confirm all three `POSTGRES_*` values are non-empty.
- **Redis connection is refused:** start Compose and verify `docker compose exec redis redis-cli
  ping` returns `PONG`.
- **GitHub returns 403/429:** wait until the reported reset time or provide `GITHUB_TOKEN`.
- **Large Simplify README files:** GitHub omits inline contents above 1 MB. Full sync
  automatically retrieves the immutable file blob by SHA, preserving the requested
  revision and UTF-8 validation.
- **Full sync returns 404 for a Markdown file:** target paths apply to every selected
  repository. `New-Grad-Positions` has no `README-Off-Season.md`; sync `README.md`
  across both repositories, then run an additional sync with
  `--repo Summer2027-Internships --target-readme README-Off-Season.md`.
- **Off-season jobs show a role as the company or a term as the location:** upgrade
  to the corrected HTML parser before syncing. It respects table headers and the
  extra Terms column, and keeps nested location tables inside their outer cell.
  Re-syncing alone cannot remove old records with incorrect identity hashes. Back
  up the database, audit source provenance and application/email references, then
  repair only confirmed malformed records and their deduplication keys. Keep
  `NOTIFICATIONS_SUPPRESS_DISCOVERY=true` and `NOTIFICATIONS_SEND_ENABLED=false`
  during a baseline repair and reload.
- **Alembic updated SQLite unexpectedly:** export `DATABASE_URL` in the same shell before the
  command; otherwise the documented SQLite fallback is used.
- **Dependencies or commands are missing:** run `poetry install --with dev`, then prefix project
  tools with `poetry run`.
