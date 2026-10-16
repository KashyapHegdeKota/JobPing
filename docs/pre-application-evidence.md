# Sponsorship and base pay before applying

The independent evidence worker reads public employer postings and application
instructions. It never fills controls, uploads a resume, logs in, solves verification
or submits. Terms behind an answer-dependent step remain unknown. Existing email
matching, durable notifications and job identity hashes are unchanged.

## Evidence and screening contracts

Checks and document revisions are keyed by job occurrence. Public snapshots contain
bounded text, source/final URLs, identity, checksums, extraction version and supporting
quotes/spans. Hidden controls, values, cookies and candidate profiles are excluded.
Only the latest complete revision supports current facts; an absent statement withdraws
support, while a failed fetch preserves history without renewing verification.
Company policy pages remain contextual in rules-1; only role documents establish
positive screening facts. Employer filings and aggregator flags never establish a match.
Generic employer pages require exactly one JobPosting with the current URL, title
and reviewed hiring entity; only that record's description/pay supports facts.
ATS page prose is limited to recognized description/application containers and
exact-role structured descriptions. Navigation, footers and unrelated/recommended
jobs are excluded. Unproven role scope requires review rather than whole-page parsing.

Screening distinguishes CPT, OPT, STEM OPT, current and future sponsorship, and
explicit citizenship/permanent-authorization restrictions. Questions do not answer
themselves. Exceptions, conflicts and unfamiliar wording require review. Sponsorship
without explicit future timing does not establish future support.
Conditional citizenship/permanent-authorization restrictions require review even
alongside an allowed training policy. Conflicting restriction statements remain
conflicting across documents, independent of source ordering.

Pay compares employer-stated base wages only, in the original currency and period.
No estimates, currency conversion or annualization. Greenhouse cents alone never
establish a period; an explicit range title or attached hourly/annual base-pay statement
may establish it. Structured and description extraction run independently. Different
amounts for the same scope remain conflicting. Generic/ambiguous locations require review.

A private profile selects current authorization, future sponsorship, exact employer
US location labels, internship hourly USD and new-grad annual USD targets. Blank
locations accept all established US locations; at least one requirement must be saved
before personalized matches exist. Maximum-based matching is intentional: $80k-$120k
reaches a $100k target, but the offer could be $80k. A starting amount below the target
with no maximum remains unresolved. No automatic law/minimum-wage benchmark is used.

Closed roles are excluded. Otherwise only a current complete check under 24 hours
old may produce a match or an established mismatch. Unknown/conditional/conflicting,
non-US or inaccessible evidence appears in Needs review. The check before applying
requests a refresh once the complete check is at least one hour old, coalescing pending
work. Manual continuation remains available. Screening is not an eligibility guarantee.

## APIs and privacy

- `GET/PUT/DELETE /api/v1/me/screening-profile`: private requirements, caller-owned.
- `GET /api/v1/me/jobs?bucket=matches|review|excluded`: server filtering/counts/pagination,
  optional `search`, exact `company`, and `active`; reasons use the same policy as SQL.
- `GET /api/v1/jobs/{id}/evidence`: cached public evidence; no fetch or private profile.
- `POST /api/v1/me/jobs/{id}/evidence-check`: existing open job only, cached result or
  202 pending status; no arbitrary URL input.

Private routes use verified Firebase identity and the existing quota-before-SQL
dependencies. Profile bodies and reasons never enter analytics or public URLs.
Validation errors are generic and do not echo private inputs. Responses are no-store.
PUT is serialized/idempotent for unchanged requirements; DELETE removes the account
profile. Profiles are independent of notification subscribers and local candidate JSON.
The existing public lower-bound minimum-pay filter retains its old meaning.

## Worker, browser isolation and limits

The worker commits queue writes with ingestion, so claims see only durable jobs.
Backfill adds at most 50 newest unverified open jobs per tick; it creates no discoveries
or emails. Claims expire after 90 seconds, checks have a 60-second deadline, and token
fencing plus locked occurrence/URL/location comparisons reject stale completions.
Material evidence changes update compatibility details and publish metadata after
commit. Unchanged checks update verification separately, leaving posting timestamps.

Two checks run concurrently, with one request per source domain. Each GET has a
10-second timeout, at most five redirects, a 2 MB body limit and bounded transient
retries. 429/503 cool down the domain for 60 seconds; failed checks wait one hour.
Three documents maximum: exact role, public landing, directly linked company policy.

The separate renderer image has no env file, mounts, DB/Firebase/candidate secrets,
or host networking. Its port is exposed only on host loopback. Chromium's own network
uses an unreachable proxy; intercepted GETs are fulfilled by the public fetcher.
Other methods, WebSockets and service workers are blocked. The fetcher resolves and
validates all addresses then connects to a pinned public IP with original TLS SNI,
preventing DNS-rebinding and redirect access to private/VM services. Each render uses
a fresh context, 40-second deadline and at most 60 requests; one render at a time.

## Rollout and rollback

1. Back up the database and apply Alembic revision `23aa3593570b` through `upgrade head`.
2. Deploy the API and frontend with `NEXT_PUBLIC_SCREENING_ENABLED=false`.
3. Copy `deploy/evidence.env.example`, review exact employer mappings if needed, build
   the dedicated ARM64 renderer and start the production Compose `evidence` profile.
   The worker runs only with `EVIDENCE_WORKER_ENABLED=true`. An unset employer registry
   path is valid and leaves unsupported generic employers unreviewed. Create the
   host directory `/etc/jobping/evidence` before starting the worker. Optional
   `/etc/jobping/evidence/employers.json` is mounted read-only in the worker at
   `/run/jobping-evidence/employers.json`; use that container path in
   `EVIDENCE_EMPLOYERS_FILE`. The renderer has no registry or secret mount.
4. Run isolated migration, PostgreSQL and controlled browser checks, then review 50
   public postings across employers/categories/providers. Record unsupported positives,
   unresolved evidence, retrieval failures and coverage; never equate coverage with accuracy.
5. Enable the frontend flag and rebuild after inspecting queue age, provider failures,
   conflict counts, CPU/memory and ordinary feed/email health.

Heartbeat logs expose queue count/oldest time; completion logs expose bounded provider
families, persisted status, error code, duration and pay-conflict presence, never
profile contents or document text. Stop the evidence profile and disable the UI flag
to roll back; ordinary discovery, browsing and notifications continue. Keep the migration
and history for recovery. Downgrade deletes evidence/profile tables and is a separate
backed-up operator action. No production activation is required for local validation.
