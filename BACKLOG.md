# JobPing Backlog

> Every implementation agent must read this file after reading `AGENTS.md`.
> Update it when starting, completing, discovering, deferring, or materially changing work.
> Keep entries concise and actionable; do not use this file as an activity log or a copy of Git history.

## In Progress

### TEST-CI-003 Repair PR integration with master

- Branch push checks pass, but the PR merge fails on missing repost classifier imports and divergent Alembic heads inherited from master.
- Integrate current master, repair imports and migration graph, then validate the combined implementation and GitHub PR checks.
- Local validation passed: 694 tests including controlled Chromium, four service opt-in skips, 84.12% branch-inclusive coverage; Ruff, Black and lock checks pass. GitHub PR validation pending.

## Ready

### APP-SAFETY-001 Enforce review and submission evidence

- Require persisted required-field/review completion, explicit authorization and independently reported browser confirmation before submitted.
- Add structured non-sensitive execution evidence, upload receipts and failed/unknown submission reconciliation.
- Current tools trust caller reports; ready/submitted state tests do not prove these missing gates.

### APP-CONCURRENCY-001 Define claims and replay protection

- Add worker leases/atomic claiming; unique attempt rows alone do not prevent double browser ownership.
- Define versioned checkpoint ordering and request idempotency; stage regression currently succeeds and status self transitions reject.
- Decide answer-save replay semantics while preserving append-only audit history.

### APP-REPOST-001 Define per-occurrence application policy

- Current schema permits one attempt per logical job and preserves it across close/reopen.
- Define occurrence/history storage and eligibility before permitting a second application to a repost.

### APP-BROWSER-001 Validate the external browser agent before release

- Run a real Greenhouse dry run stopping at ready, then at least five heterogeneous forms.
- Validate unknown answers, uploads, user verification/CAPTCHA, browser restart and an explicitly authorized confirmation.
- Controlled fixtures exercise a deterministic test driver, not Codex Chrome perception; follow `docs/testing.md` for the evidence requirements.

## Blocked

No blocked backlog items.

## Deferred / Technical Debt

## Recently Completed

### TEST-CI-002 Complete Docker revalidation

- Status: completed
- Validation: all 669 automated tests passed together with zero failures, errors or skips on Docker Linux Python 3.12.14, with Chromium and isolated PostgreSQL 16/Redis 7 enabled; runtime 348.64 seconds and branch-inclusive coverage 82.78%.
- Quality: full Ruff, Black and dependency-lock checks passed; unrelated example changes preserved.
- Isolation: fixture schemas and Redis keys were verified empty after the run; dedicated test containers removed. Local JUnit, coverage and execution reports are under ignored `test-results/docker-retest/`.
- CI: quality jobs skip opt-in groups, while dedicated browser/service jobs enable them; remote matrix execution remains pending branch publication.

### TEST-CI-001 Application boundary and CI overhaul

- Status: completed
- Delivery: 22 focused commits on `test/ci-overhaul`, including two defects caught by real-service testing; unrelated local example changes preserved.
- Coverage: 223 added cases across queue, all lifecycle/status-stage pairs, answer/candidate/MCP contracts, durable restart, rollback/reset, duplicate discovery and controlled Chromium workflows.
- CI: Linux Python 3.12/3.13/3.14, Windows 3.12/3.14, controlled browser and PostgreSQL/Redis/MCP acceptance jobs, strict pytest configuration, 80% branch-inclusive coverage floor, JUnit/XML/HTML artifacts.
- Validation: full suites pass (`649 passed, 20 opt-in skips`); Chromium/applicant suite passes (`23 passed`); all four real-service tests pass on Linux and Windows with a compatible test loop; Ruff, Black and lock checks pass. Latest Linux coverage is 82.30%.
- Environment: initial Docker Desktop startup failure was worked around with disposable Ubuntu services; after the Windows restart, Docker validation passed all 669 cases with zero skips (TEST-CI-002).
- Remaining: remote GitHub Actions execution and branch-protection settings require branch publication/repository configuration. Product gaps and real-agent release checks are tracked above and in `docs/testing.md`.

### DB-MIGRATION-001 Clean PostgreSQL enum on initial downgrade

- Status: completed
- Discovery: the new real-service round trip failed on the second upgrade because `job_type` survived downgrade to base.
- Fix: drop the PostgreSQL enum after its table; keep SQLite behavior intact.
- Validation: isolated PostgreSQL upgrade/downgrade/re-upgrade and SQLite migration regressions pass.

### MCP-WINDOWS-001 PostgreSQL stdio event-loop compatibility

- Status: completed
- Fix: the standalone Windows stdio entry point owns a Selector `asyncio.Runner`; Linux retains the official SDK runner.
- Validation: Windows official stdio lifecycle/restart tests and real PostgreSQL/Redis/MCP acceptance pass; Linux service acceptance also passes.

### INGEST-IDENTITY-002 Cross-source location reconciliation

- Owner: ingestion
- Status: completed
- Priority: medium
- Area: ingestion
- Context: Persisted location source provenance and deterministic authority selection across ATS, ApplyGuy, Simplify, and both repository save paths. Redis classification uses the effective persisted content state.
- Acceptance criteria:
  - provenance-aware authority policy without fuzzy matching; job identity hashes remain unchanged
  - same-source changes preserve genuinely different locations such as `Austin, TX` and `Seattle, WA`
  - cross-source polling and duplicate-order regressions, legacy/unknown sources, missing locations, and provenance-only upgrades covered
  - Alembic location-source upgrade/downgrade verified against isolated SQLite
  - GPT-6 Luna implementation independently reviewed; full pytest passes (`442 passed, 4 skipped`), Ruff and Black pass
- Notes:
  - Apply migration `0006_location_source` before using the updated ingestion code.
  - Cross-run reconciliation requires database-backed ingestion; standalone classification has no persisted provenance.

### INGEST-LIFECYCLE-004 Authoritative pipeline outcomes under concurrent ingestion

- Owner: ingestion / persistence
- Status: completed
- Priority: high
- Area: ingestion / persistence
- Context: Pipeline outcomes now reflect the repository's persisted lifecycle transition, so only the transaction creating a repost occurrence reports `ROLE_REPOSTED`.
- Acceptance criteria:
  - derive pipeline outcomes from authoritative SQL persistence while preserving posting-only repository APIs
  - only the transaction creating a repost occurrence reports `ROLE_REPOSTED`
  - stale pre-read concurrency cannot duplicate repost reporting or event creation
  - refresh Redis from authoritative persisted state and preserve lifecycle/notification behavior
  - add deterministic pipeline-level stale-pre-read coverage; run targeted and full validation
- Notes:
  - A stale pre-read pipeline regression confirms one repost outcome, one repost occurrence/event, and a losing `NO_OP` outcome.
  - Full suite passed: `450 passed, 4 skipped`; Ruff and Black checks passed.

### INGEST-LIFECYCLE-003 Durable repost occurrences and notifications

- Owner: ingestion / notifications
- Status: completed
- Priority: high
- Area: ingestion / persistence / notifications
- Context: Added persisted discovery/repost occurrences and occurrence-aware notifications, then corrected review findings around weak-source ordering and Workday URL identity.
- Acceptance criteria:
  - persist immutable discovered/reposted occurrences and ATS source identity evidence for each logical job
  - classify a new stable ATS identity after confirmed closure as one repost; same-ID reopening and source/aggregator churn are not reposts
  - preserve confirmed closure through ambiguous weak open observations and retain their raw provenance
  - centralize lifecycle decisions and let locked SQL state govern repository writes and final Redis refresh
  - extract only confident Workday requisition URL tokens; title-only slugs are not stable IDs
  - reconcile multiple sources observing one open occurrence; notify at most once per subscriber and occurrence
  - preserve occurrence-aware alerts/recaps, silent historical backfill, notification suppression, and application history
  - update lifecycle docs and validate migrations, Ruff, Black, and pytest
- Notes:
  - Weak-open → authoritative-new-ATS-ID regressions cover both repository write paths, preserve posting/occurrence closure and raw provenance, and create exactly one repost occurrence/event.
  - Subscriber matching is run twice in the regression; one repost match and one alert are created for the occurrence.
  - Workday URL identity tests cover R/JR suffixes, bare requisition tokens, title-only slugs, and malformed paths.
  - No schema change was needed; notification migration roundtrip and silent backfill tests passed.
  - Full suite passed: `449 passed, 4 skipped`; Ruff and Black checks passed.

### INGEST-APPLYGUY-001 Harden ApplyGuy cross-source reconciliation

- Owner: ingestion
- Status: completed
- Priority: high
- Area: ingestion
- Context: Hardened the merged ApplyGuy integration so Redis classification and PostgreSQL persistence use the same effective canonical URL state.
- Acceptance criteria:
  - true direct/fallback alternation remains `NEW_ROLE`, then three `NO_OP` results
  - reverse-order fallback/direct alternation improves once with `ROLE_UPDATED`, then remains stable
  - existing postings are batch-loaded once per pipeline run instead of once per row
  - safe location formatting variants do not churn; material differences remain updates
  - one logical posting produces one initial discovery event
  - conservative ApplyGuy closure semantics remain unchanged
  - Ruff and Black pass; full pytest passes (`423 passed, 4 skipped`)
- Notes:
  - Broader semantic location reconciliation is tracked separately as `INGEST-IDENTITY-002`.

### DOCS-BACKLOG-001 Establish repository backlog workflow

- Owner: docs/backlog
- Status: completed
- Priority: high
- Area: infrastructure / documentation
- Context: Established a durable root backlog, mandatory agent startup integration, and a lightweight consistency test.
- Acceptance criteria:
  - create the canonical root `BACKLOG.md`
  - require every implementation agent to read and maintain it through `AGENTS.md`
  - preserve the unresolved ApplyGuy work as an actionable backlog item
  - add deterministic validation that the two workflow files remain connected
- Notes:
  - ApplyGuy is intentionally not marked complete by this documentation task.
