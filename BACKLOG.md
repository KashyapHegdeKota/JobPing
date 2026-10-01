# JobPing Backlog

> Every implementation agent must read this file after reading `AGENTS.md`.
> Update it when starting, completing, discovering, deferring, or materially changing work.
> Keep entries concise and actionable; do not use this file as an activity log or a copy of Git history.

## In Progress

No backlog items are currently in progress.

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

### TEST-CI-001 Application boundary and CI overhaul

- Status: completed
- Delivery: 22 focused commits on `test/ci-overhaul`, including two defects caught by real-service testing; unrelated local example changes preserved.
- Coverage: 223 added cases across queue, all lifecycle/status-stage pairs, answer/candidate/MCP contracts, durable restart, rollback/reset, duplicate discovery and controlled Chromium workflows.
- CI: Linux Python 3.12/3.13/3.14, Windows 3.12/3.14, controlled browser and PostgreSQL/Redis/MCP acceptance jobs, strict pytest configuration, 80% branch-inclusive coverage floor, JUnit/XML/HTML artifacts.
- Validation: full suites pass (`649 passed, 20 opt-in skips`); Chromium/applicant suite passes (`23 passed`); all four real-service tests pass on Linux and Windows with a compatible test loop; Ruff, Black and lock checks pass. Latest Linux coverage is 82.30%.
- Environment: Docker Desktop startup failed; disposable native PostgreSQL/Redis processes in Ubuntu supplied real-service validation and were stopped afterward.
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
