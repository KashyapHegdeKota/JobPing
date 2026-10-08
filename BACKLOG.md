# JobPing Backlog

> Every implementation agent must read this file after reading `AGENTS.md`.
> Update it when starting, completing, discovering, deferring, or materially changing work.
> Keep entries concise and actionable; do not use this file as an activity log or a copy of Git history.

## In Progress

No implementation items currently in progress.

## Ready

### INGEST-PORTALS-001 Cover selected companies beyond Greenhouse/Lever

- The selected 20-company list is mapped in `docs/direct-ats-company-coverage.md`. Verified direct boards cover Palantir, two SK hynix America boards and Alphabet subsidiary Waymo; Google/DeepMind and the other requested employers need separate portal integrations or board discovery.
- Verify each portal and its company identity, then wire owned-resource polling with conservative eligibility and isolated acceptance tests. Existing Workday/Meta scraper classes alone do not provide scheduler coverage.
- The historical DeepMind Greenhouse API returns 404; use current Google Careers rather than the stale board. Registry defaults reject undated Palantir/SK internship/new-grad titles until a deliberate per-board season policy permits them.

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

### DISCOVERY-DETAILS-001 International-student evidence and advertised pay

- Completed: explicit CPT/OPT/STEM OPT, sponsorship/future-sponsorship evidence, dated official company history imports, employer-posted compensation and full-feed API/UI filters. Unknown/conflicting data remains explicit; employer history never grants role eligibility. Salary estimates are excluded by user choice.
- Added migration `a7b83c96c247`, occurrence-scoped details and metadata-only NO_OP refreshes without hash/discovery-event changes. Confirmed reposts preserve prior evidence and start a fresh projection; live metadata publishes after SQL commit. Exact reviewed legal-name mappings and corresponding official HTTPS hosts are required for imports.
- Validation: 820 backend tests pass, 22 opt-in skips; isolated PostgreSQL evidence filters and migration upgrade/downgrade/re-upgrade pass. Full Ruff/Black checks pass. Frontend: 105 tests, lint, type checking, production build and controlled desktop/390px phone evidence/filter review pass.
- Delivery: 15 backend and 11 frontend commits for this feature. Operational rollout still requires the migration, deployment, source repoll and reviewed official history import; no live developer database mutation or email sends were performed. See `docs/international-students-and-pay.md`. Broader company portal coverage remains in INGEST-PORTALS-001.

### INGEST-ATS-001 Activate direct Greenhouse and Lever polling

- Status: completed
- Added strict board/site registry with canonical company names, conservative title/year eligibility, mixed-category ingestion, `run-ats-sync` and real independent scheduler callbacks. Unconfigured providers report disabled; unsupported domains reject rather than silently doing nothing.
- Run resources close on success, failure and cancellation; branded application URLs preserve API token/ID identity through the centralized classifier. Repeated observations and missing rows preserve existing deduplication, occurrence/event and closure policy.
- Supplied four live-verified boards for Palantir, two SK hynix America entities and Waymo; compared applicable company names with current ApplyGuy feeds. Strict 2027 eligibility currently admits 52 Waymo rows; undated Palantir/SK roles require an explicit per-board policy. Broader company coverage remains in INGEST-PORTALS-001.
- Validation: 66 added cases; final full default suite passes (774 passed, 21 opt-in skips), Ruff, Black and diff checks pass. CLI/scheduler dry-runs pass. Live verification was read-only; no development-service ingestion or email delivery was performed.

### EMAIL-UI-001 Refresh notification presentation

- Status: completed
- Adapted supplied local alert/recap HTML to navy/mint responsive presentation tables, real escaped job data, closed-role labels, occurrence groups and existing unsubscribe/preferences links.
- No matching, scheduling, payload freezing, plain-text or delivery logic changed. The UI serves the supplied logo; browse links follow the feed's new `/jobs` UI route.
- Validation: 36 notification tests and full default suite pass (708 passed, 21 opt-in skips); Ruff and Black pass. Synthetic HTML previews generated without credentials or live sends.
- Remaining release check: verify Gmail/Outlook inbox rendering with the deployed UI logo asset; browser preview cannot replace mail-client QA.

### INGEST-SIMPLIFY-002 Preserve HTML table column identity

- Status: completed
- HTML parsing respects table headers, the off-season Terms column and nested location cells; literal ampersands, entities and location break variants preserve source text.
- Added 13 parser/pipeline regression cases; read-only audits preserved all rows in the three captured source files.
- With explicit user approval and a verified database backup/exact repair list under gitignored `private/backups`, removed 1,202 confirmed malformed jobs, 952 empty company records and only their 1,202 Redis keys. Re-loaded both main READMEs and the internship off-season file with discovery notifications suppressed and sending disabled.
- Local API/database verification: 4,383 jobs, zero term-as-location records, zero old repair IDs/keys, correct Garmin/X Development/Keysight/Harvey samples and matching sampled SQL/Redis state. No application or email history existed in the repair scope; notification events/matches/deliveries remained empty.
- Validation: full default suite passed (708 passed, 21 opt-in skips); full Ruff, Black and diff checks passed. Live reload was an approved operational repair, separate from isolated automated tests.

### INGEST-GITHUB-001 Support large README contents

- Status: completed
- Full sync fetches the immutable Git blob when the contents endpoint omits inline data for files above 1 MB; authentication, retries, UTF-8 validation and resource ownership are preserved.
- Added seven network-free regression cases for inline/large contents, unsafe or mismatched blob identity, invalid encoding/UTF-8 and blob HTTP errors.
- Read-only live checks retrieved both main READMEs and the 1.72 MB internship off-season README. The new-grad repository lacks an off-season README; documented separate target commands to avoid requesting it.
- Validation: 28 targeted tests and the full default suite passed (695 passed, 21 opt-in skips); full Ruff, Black and diff checks passed. No development database writes or live email sends were performed by validation.

### ANALYTICS-001 Site totals and private user activity

- Status: completed
- Added Firebase-authenticated activity/filter/click tracking, replay-safe events and a personal dashboard scoped to the caller; raw search text and full URLs are excluded.
- Added verified-admin aggregate site analytics for active signed-in users, unique/open jobs, reposts, subscribers and provider-accepted/confirmed email deliveries. Server-only `ANALYTICS_ADMIN_UIDS` defaults to denying admin access.
- Generated migration `0008_analytics`; SQLite and isolated PostgreSQL upgrade/downgrade/re-upgrade passed. Existing job/email totals are available immediately; activity starts with the updated UI.
- Validation: full default backend suite passed (687 tests), the additional migration test passed, all five PostgreSQL/Redis service tests and all 16 controlled browser tests passed; Ruff, Black and dependency-lock checks passed.
- UI validation: 47 tests, lint, type checking and production build passed. Live Firebase sign-in and deployed dashboard verification remain an operator rollout check.

### TEST-CI-003 Repair PR integration with master

- Status: completed
- Integrated current master; restored missing repost classifier imports and ATS result staging lost in the prior location/lifecycle integration.
- Generated `0007_merge_location_repost` to join both existing schema branches without rewriting deployed revisions; added upgrade regressions from each branch.
- Local validation: 694 tests including controlled Chromium passed, four service opt-in skips, 84.12% branch-inclusive coverage; Ruff, Black and lock checks pass.
- GitHub validation: all seven PR jobs and all seven push jobs passed for `038c358`, including real PostgreSQL/Redis/MCP and controlled browser acceptance (PR run `36938227512`).

### TEST-CI-002 Complete Docker revalidation

- Status: completed
- Validation: all 669 automated tests passed together with zero failures, errors or skips on Docker Linux Python 3.12.14, with Chromium and isolated PostgreSQL 16/Redis 7 enabled; runtime 348.64 seconds and branch-inclusive coverage 82.78%.
- Quality: full Ruff, Black and dependency-lock checks passed; unrelated example changes preserved.
- Isolation: fixture schemas and Redis keys were verified empty after the run; dedicated test containers removed. Local JUnit, coverage and execution reports are under ignored `test-results/docker-retest/`.
- CI: quality jobs skip opt-in groups, while dedicated browser/service jobs enable them; the published branch and integrated PR matrix passed (TEST-CI-003).

### TEST-CI-001 Application boundary and CI overhaul

- Status: completed
- Delivery: 22 focused commits on `test/ci-overhaul`, including two defects caught by real-service testing; unrelated local example changes preserved.
- Coverage: 223 added cases across queue, all lifecycle/status-stage pairs, answer/candidate/MCP contracts, durable restart, rollback/reset, duplicate discovery and controlled Chromium workflows.
- CI: Linux Python 3.12/3.13/3.14, Windows 3.12/3.14, controlled browser and PostgreSQL/Redis/MCP acceptance jobs, strict pytest configuration, 80% branch-inclusive coverage floor, JUnit/XML/HTML artifacts.
- Validation: full suites pass (`649 passed, 20 opt-in skips`); Chromium/applicant suite passes (`23 passed`); all four real-service tests pass on Linux and Windows with a compatible test loop; Ruff, Black and lock checks pass. Latest Linux coverage is 82.30%.
- Environment: initial Docker Desktop startup failure was worked around with disposable Ubuntu services; after the Windows restart, Docker validation passed all 669 cases with zero skips (TEST-CI-002).
- Remaining: branch-protection settings require repository configuration. Remote GitHub Actions execution passed after publication and master integration (TEST-CI-003). Product gaps and real-agent release checks are tracked above and in `docs/testing.md`.

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
