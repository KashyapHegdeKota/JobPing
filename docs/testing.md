# Automated testing and release evidence

The overhaul tests the implemented application lifecycle, persistence boundaries,
official MCP transport and controlled Chromium fixtures. It does not certify the
behavior of an external Codex Chrome agent or a live employer form.

## Everyday checks

```text
poetry install --with dev
poetry check --lock
poetry run ruff check .
poetry run black --check .
poetry run pytest -q --strict-markers --strict-config --basetemp=.pytest-tmp
```

The explicit temporary directory also avoids Windows permission errors in an
old shared pytest temp directory. Pytest deletes its selected `--basetemp` at the
start of a run: use this disposable directory only, never a project/data directory.
Use a different temporary directory for concurrent test processes.

Coverage includes branches and has an 80% minimum when requested:

```text
poetry run pytest -q --basetemp=.pytest-tmp --cov=app --cov-branch --cov-report=term-missing:skip-covered --cov-report=xml --cov-report=html --junitxml=test-results/quality.xml
```

Open `htmlcov/index.html` for missed branches. MCP child processes are tested over
stdio, but this coverage configuration measures the parent process only. A low
server-module percentage therefore does not mean its transport is untested.
Python coverage cannot measure embedded DOM JavaScript; Chromium tests exercise it.

## Coverage map

| Boundary | Automated evidence |
| --- | --- |
| Discovery → queue | Repeated requests, closed/missing jobs, tied timestamps, all excluded statuses |
| Lifecycle | All 64 status pairs; rejected self transitions are explicit current behavior |
| Checkpoints | All 80 status/stage pairs, invalid input, safe projections |
| Candidate/answers | Unknown facts require review, bounded story selection, narrow facts, unavailable resumes, invalid answers and OTP rejection |
| Restart | File-backed SQLite sessions rebuilt during progress, verification, review and ready; answers and attempt identity retained |
| Persistence | Caller rollback, unique job-attempt constraint, reset/retry audit retention |
| Discovery identity | Both save paths; duplicate source observations yield one posting/event/attempt; close/reopen retains submitted history |
| MCP | Official stdio discovery → start → pause/resume → ready; independent server process restart |
| Browser | Real Chromium DOM inspection, text/select/radio/checkbox/multiselect/textarea/upload, review, explicit synthetic confirmation |
| Browser failures | Empty/wrong/oversized uploads, missing facts, user-completed verification/CAPTCHA, validation/network/unknown confirmation outcomes |
| Real services | Isolated PostgreSQL migration round trip and restart; concurrent Redis Lua classification and TTL refresh; mocked Greenhouse → real Redis/PostgreSQL → stdio MCP → ready |

## Controlled browser acceptance

Install Chromium explicitly, then enable the browser suite:

```powershell
poetry run playwright install chromium
$env:RUN_BROWSER_E2E = "1"
poetry run pytest tests/e2e tests/applicants -m browser_e2e -q --basetemp=.pytest-tmp
Remove-Item Env:RUN_BROWSER_E2E
```

On Linux use `RUN_BROWSER_E2E=1 poetry run pytest ...`. CI installs Chromium and
Linux dependencies automatically. New application pages intercept every browser
request and serve a local HTML fixture; unmatched requests are aborted. All
candidate values and attachments are synthetic. Filling/clicking exists only in
the test harness. No production Playwright application automation is introduced.

The failure tests deliberately withhold the MCP submitted call when browser
confirmation is absent. They verify that supported orchestration is possible;
they cannot prove an external agent will make that decision correctly.

## Isolated PostgreSQL and Redis acceptance

CI uses disposable PostgreSQL 16 and Redis 7 services. Locally provision a
dedicated loopback PostgreSQL database named `jobping_test*`, with permission to
create schemas, and a dedicated loopback Redis instance using database 15. Do not
point these tests at the normal development services.

```powershell
$env:RUN_SERVICE_INTEGRATION = "1"
$env:JOBPING_TEST_DATABASE_URL = "postgresql+psycopg://jobping:test-only-password@127.0.0.1:55432/jobping_test_local"
$env:JOBPING_TEST_REDIS_URL = "redis://127.0.0.1:56379/15"
poetry run pytest tests/services -q --basetemp=.pytest-tmp
```

Run PostgreSQL tests on Linux/WSL. On Windows, psycopg's async driver needs a
Selector event loop; the general pytest/browser suite retains its normal loop
because browser subprocesses need Windows Proactor support. CI service tests run
on Linux. The production Windows MCP stdio entry point owns a Selector runner;
the pytest service fixtures still require a compatible loop if run on Windows.
Tests never read the ordinary `DATABASE_URL` for fixture provisioning.
Each test creates and drops a UUID schema. Redis uses UUID key namespaces and
deletes only its own keys; there is no `FLUSHDB`. Invalid fixture URLs fail before
connecting. A process forcibly killed during testing may leave a test schema/key;
cleanup must be limited to the dedicated test services.

## CI checks and reports

Pull requests, pushes to `main`, `master`, `test/ci-overhaul`, and manual dispatch
run the workflow. Quality runs on Ubuntu with Python 3.12/3.13/3.14 and Windows
with Python 3.12/3.14. Separate jobs enable controlled browser acceptance and real
service acceptance. Sending notifications stays disabled.

JUnit XML and coverage XML/HTML are retained as artifacts for 14 days, including
on failure when reports were produced. Configure repository branch protection to
require all quality matrix jobs, `Controlled browser acceptance` and
`PostgreSQL Redis MCP acceptance`; the workflow itself cannot configure that
repository setting. No credentials or employer access are needed.

## Open product gaps and manual release gate

Current backend contracts are deliberately documented without treating the
attached aspirational plan as an implemented feature specification:

- `READY_TO_SUBMIT` trusts the caller; it has no persisted required-field/review
  evidence gate. `application_mark_submitted` requires the ready state, but its
  confirmation URL is optional and it does not verify explicit authorization or
  browser evidence. A real submission is not certified by this test suite.
- Answer storage is append-only audit history; repeated saves add records.
  Status self transitions reject rather than silently succeed. Retry/idempotency
  contracts need a product decision before implementation.
- Checkpoint stage updates can regress within an active status; no sequence or
  optimistic version protects against stale/concurrent browser reports.
- The queue selects eligible work but does not lease or atomically claim it.
  Database uniqueness prevents duplicate attempt rows, not double browser ownership.
- There is one attempt per logical job. Closing/reopening preserves that history;
  a new per-occurrence application policy/history model is not implemented.
- Structured execution evidence, upload receipts, confirmation outcome categories,
  and browser crash reconciliation are not implemented in the backend.
- Resume tools validate availability and `.pdf` extension. Empty/oversized/browser-
  rejected uploads are exercised by the fixture, not globally prevented by the tool.

Before a browser-agent release, manually validate at least five heterogeneous
Greenhouse forms: a simple form, custom questions, dropdowns, self-identification,
and resume plus cover letter. Record field accuracy, wrong answers, unresolved
questions, intervention count and upload evidence. Verify unknown questions pause,
user verification/CAPTCHA completion, browser restart reconciliation and that ready
does not submit. A deliberately authorized real submission must have independently
observed confirmation and a non-sensitive evidence record. Stop dry runs at ready.
Lever and Workday application automation remain outside this acceptance suite.
