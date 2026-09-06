# JobPing

## Application Inspection

JobPing can inspect supported ATS application forms without modifying or submitting them:

```text
poetry run python -m app.cli inspect-application <job-id>
```

Currently supported: Greenhouse inspection. Lever and Workday inspection are planned.
The `private/` directory is intentionally gitignored; use the sanitized files in
`examples/` as a starting point for local candidate configuration.

## Autonomous application backend (Phase 2)

The application backend stores one durable checkpoint per job in
`application_attempts` (including its current browser URL and coarse checkpoint stage)
and non-secret answer audit records in `application_answers`. Apply the
`0002_application_agent` and `0003_application_checkpoint_stage` Alembic migrations
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

The CLI supports fetching and classifying Simplify commits (with database persistence),
running a persistent scheduler daemon, and auditing the database for anomalies.

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

- **Compose reports blank PostgreSQL variables:** create `.env` from `.env.example` before
  running `docker compose up`.
- **Port 5432 or 6379 is already allocated:** stop the conflicting local service or change the
  host-side port and update the corresponding connection URL.
- **A container is unhealthy:** inspect `docker compose ps` and
  `docker compose logs postgres redis`; confirm all three `POSTGRES_*` values are non-empty.
- **Redis connection is refused:** start Compose and verify `docker compose exec redis redis-cli
  ping` returns `PONG`.
- **GitHub returns 403/429:** wait until the reported reset time or provide `GITHUB_TOKEN`.
- **Alembic updated SQLite unexpectedly:** export `DATABASE_URL` in the same shell before the
  command; otherwise the documented SQLite fallback is used.
- **Dependencies or commands are missing:** run `poetry install --with dev`, then prefix project
  tools with `poetry run`.
