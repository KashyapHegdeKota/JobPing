# Paused sponsorship/pay implementation

Paused explicitly by the user on October 12, 2026. Resume the implementation from
this checkpoint; do not restart from the original plan. Nothing was committed,
published, migrated into development/production services, or deployed.

Read `AGENTS.md` and `BACKLOG.md` completely on resumption, plus the sibling UI's
workflow files. The implemented architecture is documented in
`docs/pre-application-evidence.md`. Backend work is DISCOVERY-CHECK-001; frontend
work is UI-CHECK-001 in `../JobPing-ui/jobping-ui`.

## Implemented

- `app/evidence/`: strict private requirements, versioned deterministic extraction,
  reconciliation, maximum-based pay screening, matching SQL predicates, public-only
  pinned-IP retrieval, Greenhouse/Lever and reviewed generic employer adapters.
- Three ORM tables and generated Alembic revision `23aa3593570b`: profiles, durable
  occurrence checks, public document revisions. Checks have token-fenced leases,
  source fingerprints, target/repost fencing, separate freshness and partial snapshots.
- Ingestion queues checks in its transaction when the worker flag is enabled.
  Completed checks own compatibility details; unchanged source fingerprints avoid
  parser-version oscillation. Material metadata publishes after commit; no new emails.
- Private profile CRUD, private paginated matches/review/excluded feed, cached public
  evidence and coalescing job-ID-only refresh APIs. Verified account ownership, quota
  dependencies, no-store responses and validation without echoing private inputs.
- Independent CLI worker; two checks concurrently, domain serialization, bounded
  GET/retries/cooldowns, three documents, 60-second overall deadline.
- Secret-free isolated renderer: intercepted pinned-public GETs, blocked writes,
  WebSockets/service workers, fresh contexts, bounded text/resources. Dedicated Docker
  image exports a minimal dependency closure from poetry.lock; Compose profile and
  `deploy/evidence.env.example` keep activation separate and disabled by default.
- Frontend profile form, server-counted screening buckets, card summaries and
  pre-application dialog with cached/refresh states, source quotes and manual continue.
  Requirements do not enter public URLs, analytics or notification preferences.
- Docs/README/AGENTS and both backlogs updated. Existing public lower-bound salary
  filter, notification matching and ingestion hashes retain their contracts.

## Verified checkpoints

- Full backend run: **953 passed, 30 opt-in skips**, **83.97% branch-inclusive coverage**,
  saved as ignored `test-results/evidence-full-2.xml`. This predates the last explicit
  annual-prefix parsing regression and minor source-question cleanup.
- Latest targeted run after those edits: **54 passed** across evidence extraction,
  adapters, worker, persistence and private APIs. Latest Ruff/Black checks passed;
  rerun full checks on the final code before delivery.
- Isolated PostgreSQL: four SQL/Python screening parity cases, concurrent single-owner
  claim, and complete migration upgrade/downgrade/re-upgrade passed (six tests).
  These ran before the last source-fingerprint/version refinements; rerun them.
- Controlled Chromium renderer: reads labels without filling/submitting, excludes
  candidate/hidden values, and blocks page-initiated POSTs; one test passed.
- Frontend: **135 tests passed**, lint/type checks and production builds passed.
  The last enabled build used `NEXT_PUBLIC_SCREENING_ENABLED=true` for fixture QA.
  Production/Preview account settings were never changed.
- Desktop 1440x1000 and phone 390x844 fixture feed/dialog screenshots passed overflow
  checks and were visually inspected. Saved under ignored `private/evidence-ui`.

## Public-source review and discoveries

Ignored `private/evidence-shadow-20261012-v2` contains 50 sampled public postings
from Waymo, Databricks, Figma, Stripe and Palantir, both role categories and both ATS
providers. Replay with the finalized October 16 extraction and location rules
(`private/screening-replay-final.json`) yields **15 verified salary matches** (1 Waymo,
3 Databricks, 8 Figma, 3 Stripe), **27 conservative review outcomes** (20 non-US or
multi-country postings, 2 Databricks new-grad roles with unitless `Local Pay Range`,
1 Figma new-grad role with hourly pay, 2 Stripe internships with annual-only pay, and
1 Palantir internship with monthly total compensation), and 8 historical Waymo HTTP
wrapper failures from the October 12 capture. Fresh collection now falls back to the
verified `boards-api.greenhouse.io` document when an external branded Greenhouse wrapper
is an unverified client-side shell or returns HTTP/transport unavailability.

All 15 matched salary bounds, USD currency, explicit hourly/annual statements and US
locations were inspected against their captured quotes with zero unsupported positives.

## Completed October 16 fixes

1. **Contrastive clause splitting & sponsorship polarity (`app/services/job_details.py`, `app/evidence/extract.py`)**:
   - Split sentences on contrastive conjunctions (`but`, `however`, `although`, `though`, `while`, `whereas`) while preserving exact character offsets (`spans`) in `extract()`.
   - Added adverbial and direct verb negations (`do not currently offer`, `cannot sponsor`, `without visa sponsorship`, `not eligible for employment visa sponsorship`, `requiring visa sponsorship are not eligible`) and restricted bare `sponsor` matches to immigration/visa contexts so `executive sponsor` / `event sponsor` do not false-positive.
2. **Shared US location & currency validation (`app/services/job_details.py`, `app/evidence/screening.py`, `app/evidence/extract.py`)**:
   - Consolidated `US_STATES`, `US_STATE_NAMES`, `US_METROS`, `_US`, `_FOREIGN`, and `us_location()` so spelled-out US state names (`San Francisco, California`, `Bellevue, Washington`) and unambiguous US tech metros (`SF Bay Area`, `Seattle, San Francisco`) are recognized while foreign/multi-country locations (`Toronto, CA`, `San Francisco, California; Vancouver, Canada`) are excluded.
   - Preserved `document.location` on `_PREFIX_PAY` ranges when `us_location(document.location)` is true so location filtering matches `profile.locations`.
3. **Greenhouse `pay_input_ranges`, Stripe scope & branded wrapper fallback (`app/evidence/collect.py`, `app/evidence/extract.py`, `app/services/job_details.py`)**:
   - Recognized explicit unit words with degree/level modifiers (`Hourly Bachelors Pay`, `SF Bay Area Hourly Rate`, `expected hourly rate`) while keeping `annual bonus` / `annual performance bonus` as `unknown` and sales/OTE roles as `unspecified`.
   - Added `careers-listing-details__body` to `ROLE_CLASSES` for Stripe listing pages and included Greenhouse `location_questions` and `compliance` questions.
   - Preserved the verified `boards-api.greenhouse.io` document when a secondary branded corporate wrapper is an unverified JS shell or returns HTTP/transport errors.
4. **Persistence, worker & UI parity (`app/db/repository.py`, `app/evidence/worker.py`, `deploy/compose.production.yml`, `src/lib/screening.ts`)**:
   - Checked `check.verified_at is not None` and full target identity (`url`, `location`, `title`) in `_merge_job_details` before skipping ingestion detail merges.
   - Added bounded status counts (`queued`, `complete`, `partial`, `failed`) to `evidence.heartbeat` and aligned the Compose volume mount `/etc/jobping/evidence:/etc/jobping/evidence:ro`.
   - Preserved `company.immigration_records` in `parseScreenedPage` so `ScreenedFeed` cards render official DOL/USCIS employer history alongside role evidence.

## Local services and preservation

Stopped for the pause: UI dev server on 3006, production fixture server on 3007,
ARM64 Docker build, and the owned PostgreSQL test container. The stopped container
`jobping-evidence-test-pg` is retained for resumption (loopback port 5548, database
jobping_test_evidence, isolated UUID schemas). Test-only credentials were specified
in the prior test commands. Do not point tests at normal development services.

On Windows psycopg service tests need a Selector runner. The previous successful
invocation supplied a Selector loop through pytest-asyncio's runner; default Proactor
failed before exercising SQL. Use a compatible runner or the Linux service CI.

Preserve pre-existing changes in `.env.example`, the three candidate/application
example JSON files, old temporary test directories, and UI parent `.env.local`/
`.worktrees`. The pre-existing `.env.example` edit contains a credential: do not
publish/commit that edit; rotate it before publishing the file. Never copy the value
into logs, this handoff, examples, test fixtures or generated artifacts.
