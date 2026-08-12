# JobPing

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
| `GITHUB_REPO` | `Summer2026-Internships` | CLI | Target repository name. |
| `GITHUB_REF` | `HEAD` | CLI | Commit SHA, tag, or branch to process. |
| `TARGET_README` | `README.md` | CLI | Exact Markdown path inspected in the commit. |
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
