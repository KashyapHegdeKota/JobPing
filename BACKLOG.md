# JobPing Backlog

> Every implementation agent must read this file after reading `AGENTS.md`.
> Update it when starting, completing, discovering, deferring, or materially changing work.
> Keep entries concise and actionable; do not use this file as an activity log or a copy of Git history.

## In Progress

No items currently in progress.

## Ready

## Blocked

No blocked backlog items.

## Deferred / Technical Debt

### INGEST-IDENTITY-002 Cross-source location reconciliation

- Owner: unassigned
- Status: deferred
- Priority: medium
- Area: ingestion
- Context: Content hashing safely normalizes Unicode, case, and whitespace, but materially different source descriptions such as `Remote` and `Remote, U.S.` remain distinct and may produce legitimate `ROLE_UPDATED` classifications.
- Acceptance criteria:
  - define a provenance-aware, deterministic location authority policy without fuzzy matching
  - preserve genuinely different locations such as `Austin, TX` and `Seattle, WA`
  - add cross-source regression coverage before changing identity semantics

## Recently Completed

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
