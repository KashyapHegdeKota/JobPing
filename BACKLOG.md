# JobPing Backlog

> Every implementation agent must read this file after reading `AGENTS.md`.
> Update it when starting, completing, discovering, deferring, or materially changing work.
> Keep entries concise and actionable; do not use this file as an activity log or a copy of Git history.

## In Progress

No backlog items are currently in progress.

## Ready

### INGEST-APPLYGUY-001 Add ApplyGuy 2027 feeds

- Owner: unassigned
- Status: ready
- Priority: high
- Area: ingestion
- Context: The current branch does not contain an ApplyGuy source implementation. Add ApplyGuy's 2027 internship and new-grad JSON repositories as first-class discovery sources while preserving one logical posting across ApplyGuy, Simplify, and direct ATS sources.
- Acceptance criteria:
  - ingest `data/internships.json`
  - ingest `data/new-grad-jobs.json`
  - prefer `listingUrl`
  - prevent cross-source duplicate tracker entries
  - prevent URL source ping-pong
  - integrate CLI and scheduler
  - preserve conservative closure semantics
  - add cross-source regression tests
  - update `README.md` and `AGENTS.md`
  - Ruff, Black, and pytest pass
- Notes:
  - Related ApplyGuy commits exist on separate branches, but this item remains unresolved until the implementation is merged and verified against these criteria.

## Blocked

No blocked backlog items.

## Deferred / Technical Debt

No deferred items recorded.

## Recently Completed

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
