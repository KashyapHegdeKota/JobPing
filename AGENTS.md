# JobPing Agent Guide

> **Mandatory first step:** Every agent must read this entire `AGENTS.md` before writing code, changing dependencies, creating migrations, or making commits. Re-read it after context compaction. Inspect the current implementation and tests before assuming a contract; update this file when architecture materially changes.

## Purpose and architecture

JobPing is a Python 3.12, async-first discovery engine for 2026/2027 internships and new-grad roles. Its flow is:

`sources -> scraper/parser -> RawJobPayload -> normalization + hashes -> Redis classification -> SQLAlchemy repository -> PostgreSQL`

Current source families are Simplify GitHub README diffs/full sync, Greenhouse and Lever JSON APIs, and Playwright-based Workday/Amazon/Meta portals. FastAPI delivery, Redis Pub/Sub, WebSockets, and SSE are Phase 3 work unless their code is already present when you read this.

Key directories:

- `app/scrapers/`: source clients, parsers, Playwright/browser/proxy/network capture.
- `app/applicants/`: read-only, ATS-specific application-form detection and inspection.
- `app/applications/`: application inspection orchestration over repository and browser boundaries.
- `app/candidate/`: private candidate profile schemas and local JSON loading.
- `app/pipelines/`: orchestration from raw records through deduplication and persistence.
- `app/services/`: hashing, Redis deduplication, and database audit logic.
- `app/db/`: SQLAlchemy 2.0 models and async repository.
- `app/schemas/`: Pydantic v2 ingestion and normalized DTOs.
- `app/utils/`: retries, per-domain rate limiting/user agents, and in-memory metrics.
- `app/scheduler.py`, `app/scheduler_parallel.py`: polling and failure-isolated concurrent execution.
- `app/cli.py`: Typer commands and owned-resource lifecycle.
- `alembic/`: migrations; `tests/unit`, `tests/integration`, and opt-in `tests/e2e` mirror risk levels.

## Data contracts and schema

`RawJobPayload` is permissive (`extra="allow"`) because source data is irregular. `NormalizedJob` is strict (`extra="forbid"`), accepts only seasons 2026/2027, requires a valid HTTP URL, canonical `internship`/`new_grad`, lowercase 64-character hashes, and timezone-aware timestamps.

PostgreSQL schema (migration `0001_initial_schema.py`):

- `companies`: integer PK; unique, required `name`; unique nullable `domain`; timezone-aware `created_at`.
- `job_postings`: integer PK; `company_id` FK with `ON DELETE CASCADE`; title, apply URL, location, season, job type, closed flag, timestamps; unique `base_hash`; indexed `content_hash`; discovery index on `(season, job_type, is_closed)`; season DB constraint 2020-2100 (the application is intentionally stricter).
- `status_logs`: integer PK; `job_id` FK with `ON DELETE CASCADE`; nullable previous state, required new state, changed timestamp; index on `(job_id, changed_at)`.

ORM enums store lowercase values. `DatabaseRepository` joins an existing session transaction or opens one when idle; it flushes but does not commit a caller-owned transaction. Company upsert keys on name, job upsert keys on `base_hash`, and status logs must represent a real transition.

## Identity, state, and deduplication

Never reproduce hash logic ad hoc; call `app.services.hasher`.

- Base identity: normalize company and title with Unicode NFKC, case folding, punctuation/underscore removal, whitespace collapse, then SHA-256 an unambiguous length-prefixed composition.
- Content state: validate/canonicalize the base hash; normalize URL scheme/host, location NFKC/case/whitespace, and boolean text; SHA-256 the length-prefixed `(base_hash, apply_url, location, is_closed)` composition.
- Redis key: `jobping:dedupe:{base_hash}`, default TTL 90 days.
- One Lua operation atomically compares, refreshes TTL, and updates content state: missing -> `NEW_ROLE`; equal -> `NO_OP`; changed and open -> `ROLE_UPDATED`; changed and closed -> `ROLE_CLOSED`.

Redis classification and PostgreSQL writes are separate systems, not one distributed transaction. The ATS pipeline classifies before SQL persistence; a SQL failure can leave Redis ahead until reconciliation or TTL expiry. Do not publish externally visible events before the SQL transaction is safely persisted. Full Simplify sync may persist `NO_OP` rows to repair an empty database behind a warm cache.

## Application inspection

Phase 1 supports read-only Greenhouse application inspection through `python -m app.cli inspect-application <job_id>`. `ApplicationService` loads an open job through `DatabaseRepository`, detects its ATS, owns the injected `BrowserManager` lifecycle, navigates to the application URL, and delegates DOM normalization to `GreenhouseApplicant`. Lever and Workday are detected but intentionally rejected as unsupported for inspection.

Normalized application forms use ATS-independent schemas in `app/schemas/application.py`. Inspection may read controls, labels, required markers, and options, but must never fill, select, check, upload, click submission controls, or submit a form. Candidate data belongs under the gitignored `private/` directory; loaders must validate it without logging its contents and must verify the configured resume path exists.

The autonomous application backend uses `app/mcp/` for high-level business tools
and `app/applications/` for deterministic queue, answer resolution, and state
transitions. `ApplicationAttempt` and `ApplicationAnswer` are persisted by the
`0002_application_agent` migration; `0003_application_checkpoint_stage` adds the
coarse `checkpoint_stage` resume field. Codex Chrome owns live browser perception and
interaction; JobPing must not add application Playwright filling, submission, OTP
retrieval, CAPTCHA bypass, or secret storage. Verification checkpoints store only
type, URL, and a user instruction. `applications.auto_submit` defaults to false.
Phase 2 also exposes `application_update_checkpoint`, narrow candidate fact tools, and
`stories_get`; application submission remains valid only from `READY_TO_SUBMIT` and must
follow explicit browser confirmation.

## Ingestion and scraper behavior

- `BaseScraper` owns a client only when it creates it, records run timing/count/success, and exposes async cleanup/context-manager behavior. Preserve caller ownership for injected clients.
- GitHub follows redirects, resolves the default/latest commit safely, handles rate limits, extracts only target README patches, and supports full-file seeding from Simplify's live `dev` branch by default. Patch parsing excludes diff metadata. Markdown parsing tolerates changing columns, continuation company marker `↳`, HTML rows/tags, varied links, multiple locations, lock emoji, strikethrough, and explicit closed status.
- Greenhouse and Lever consume public JSON endpoints and return `RawJobPayload`; malformed individual rows should be logged/rejected without silently losing the whole response.
- `BrowserManager` provides Playwright contexts with randomized profiles, stealth application, optional proxy attachment, and configurable resource blocking. Browser hardening does not guarantee bypass of bot controls.
- `ProxyManager` loads `PROXY_LIST`, rotates healthy endpoints, cools down 403/429/503 failures, and must never expose credentials in logs or errors.
- Workday paginates rendered pages. Amazon/Meta custom scrapers prefer bounded, deduplicated XHR/fetch JSON captured by `NetworkInterceptor`, with DOM fallback.
- `external_retry` retries transient I/O with bounded exponential backoff and jitter. `DomainRateLimiter` uses token buckets; `UserAgentRotator` supplies dynamic headers. Keep retries outside parsing/validation failures.
- `ScraperMetrics` records in-memory execution, parsed-job, error, and proxy-health counters. Avoid labels with unbounded cardinality or secrets.
- `SchedulerDaemon` registers non-overlapping UTC interval jobs. Parallel execution uses `asyncio.gather(..., return_exceptions=True)` semantics, preserves input order, isolates failures/timeouts, and propagates cancellation after draining children.

## Resource lifecycle and Windows

Close every owned `httpx.AsyncClient`, Redis client, SQLAlchemy engine, Playwright page/context/browser, and background listener deterministically (`async with` or `finally`). Do not close injected resources. HTTP clients should use explicit timeouts and `follow_redirects=True` where redirects are expected. Never swallow JSON/Pydantic mapping errors; log useful source context without payload secrets.

On Windows, CLI async entry points use `app.cli._asyncio_run`, which selects a `SelectorEventLoop` for psycopg compatibility. Avoid module-level async clients or resources bound to a prior `asyncio.run()` loop; construct and close them inside the active async lifecycle. Console output must tolerate legacy encodings.

## Configuration and commands

Copy `.env.example` to `.env` and keep credentials out of Git. Core variables are `DATABASE_URL`, `REDIS_URL`, `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_REF`, `TARGET_README`, `JOB_SEASON`, `JOB_TYPE`, and `PROXY_LIST`. Alembic expects a synchronous SQLAlchemy URL; runtime database code uses an async URL/driver.

Common commands:

```text
poetry install
docker compose up -d
poetry run alembic upgrade head
poetry run python -m app.cli run-simplify-parser --limit 10
poetry run python -m app.cli run-simplify-parser --full-sync
poetry run python -m app.cli run-simplify-full-sync --target-readme README.md
poetry run python -m app.cli start-scheduler --dry-run
poetry run python -m app.cli audit-db
poetry run ruff check .
poetry run black --check .
poetry run pytest
```

Browser E2E tests are opt-in (`RUN_BROWSER_E2E=1`) and require a local Chromium installed with `poetry run playwright install chromium`. Unit/integration tests must mock external networks and use isolated Redis/SQL substitutes where practical.

## Change and commit discipline

- Preserve strict typing, async cancellation, stable public contracts, deterministic ordering, and dependency injection for tests.
- Inspect related tests first; add regression tests for behavior changes. Run targeted tests while iterating, then the full Ruff, Black check, and pytest suite before handoff.
- Generate schema changes through Alembic and verify upgrade/downgrade behavior. Do not mutate a developer's live database during tests.
- Keep commits single-purpose and use the exact requested message. Do not mix generated caches, secrets, `.env`, browser binaries, or unrelated work.
- Multiple agents may share the repository: work only in the assigned worktree/files, preserve others' changes, and never reset, rewrite, or discard work you do not own.
- Update README for user-facing operation changes and this file for architectural contracts future agents must know.
