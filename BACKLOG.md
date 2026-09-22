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
- Context: Added persisted discovery/repost occurrences, conservative ATS identity evidence, and occurrence-aware notifications while keeping each role's logical posting identity stable.
- Acceptance criteria:
  - persist immutable discovered/reposted occurrences and ATS source identity evidence for each logical job
  - classify a new stable ATS identity after a confirmed closure as one repost; same-ID reopening and source/aggregator churn are not reposts
  - reconcile multiple sources observing the same open occurrence to one occurrence and event
  - match and deduplicate notifications per occurrence, with occurrence-aware frozen delivery payloads and idempotency
  - label repost alerts in subject, HTML, and text; render separate, counted new/reposted recap sections
  - backfill historical occurrences without generating new historical notification events
  - preserve bootstrap suppression, recipient checks, quotas, catch-up recaps, leases, and existing application history
  - update lifecycle/notification docs and add regression coverage
  - run migration validation, Ruff, Black, and pytest
- Notes:
  - Validated silent backfill of historical occurrences/events/deliveries and SQLite migration upgrade/downgrade roundtrip.
  - Full suite passed: `439 passed, 4 skipped`; Ruff and Black checks passed.

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
