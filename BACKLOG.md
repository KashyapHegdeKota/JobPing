# JobPing Backlog

> Every implementation agent must read this file after reading `AGENTS.md`.
> Update it when starting, completing, discovering, deferring, or materially changing work.
> Keep entries concise and actionable; do not use this file as an activity log or a copy of Git history.

## In Progress

### TEST-CI-001 Application boundary and CI overhaul

- Status: in progress
- Area: automated tests / CI
- Scope: queue/state/checkpoint/answer/MCP contracts, durable restart and discovery integration, controlled browser fixtures, PostgreSQL/Redis service tests, cross-platform CI and reporting.
- Delivery: 15–20 focused commits on `test/ci-overhaul`; preserve unrelated local changes.
- Limits: real employer dry runs and Codex Chrome behavior require a manual release gate; fixture browser tests exercise controlled pages only.

## Ready

## Blocked

No blocked backlog items.

## Deferred / Technical Debt

## Recently Completed

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
