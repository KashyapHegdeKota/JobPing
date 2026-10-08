# International-student evidence and employer-posted pay

JobPing distinguishes **role statements** from **dated employer records**. A company
with previous H-1B filings may still decline sponsorship for an individual role.
Missing evidence remains unknown. These fields do not determine a student's legal
eligibility and do not ask users to disclose immigration status.

## Role evidence

The feed shows CPT, OPT, STEM OPT, sponsorship and future sponsorship separately.
Each stated policy includes an excerpt, the application URL, source and observation
date. Values are `allowed`, `denied`, `conditional`, `conflicting`, or `unknown`.
The UI's “explicitly accepted” filters select only `allowed` roles. Screening
questions and generic “authorized to work” requirements do not establish acceptance.
STEM OPT support does not automatically establish ordinary OPT support.

Greenhouse/Lever descriptions are parsed conservatively. Other direct source
payloads can supply descriptions through the same normalization boundary. Simplify's
documented 🛂 and 🇺🇸 markers provide negative evidence only. Unstructured statements
that the parser cannot establish remain unknown; there is no AI eligibility guess.

Direct ATS evidence outranks aggregator evidence; newer same-source observations
replace older ones. Conflicting sources of equal authority retain a conflict with
both excerpts. Missing descriptions preserve known observations. An observation
date says when JobPing checked the source, not when the employer changed its policy.

CPT requires DSO authorization. OPT requires the relevant work authorization;
STEM OPT requires an eligible E-Verify employer and Form I-983 arrangements.
See [ICE practical training](https://www.ice.gov/sevis/practical-training) and
[DHS Form I-983 overview](https://studyinthestates.dhs.gov/form-i-983-overview).
An employer history record is not evidence that a specific position meets these
requirements.

## Advertised compensation

Only direct employer-posted pay is extracted. Lever's structured `salaryRange`
preserves currency, bounds and interval. Configured Greenhouse polling reads the
public detail endpoint with `pay_transparency=true` for eligible roles only; optional
detail failures preserve the board listing. Greenhouse cents convert to currency
units, but its range API does **not** establish a pay period. Such ranges explicitly
show “period not stated” and cannot satisfy a hourly/yearly minimum filter.

When structured pay is absent, explicit description ranges or fixed amounts with
currency and period can be extracted. A bare `$` requires an explicitly U.S.
location; ambiguous currency is omitted. Estimates and aggregator compensation
are excluded. There is no currency conversion or annualization of internship pay.
Location labels and base/total/unspecified compensation remain distinct.
Description formats outside the supported grammar remain unknown.

Sources: [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html),
[Lever Postings API](https://github.com/lever/postings-api).

## Official employer history imports

History is an operator-reviewed import, not a claim derived from a company name.
Accepted record kinds are `h1b_filings` (DOL certified LCA cases), `h1b_approvals`
(USCIS), `cpt_history`/`opt_history`/`stem_opt_history` (ICE), and `e_verify`.
Each record requires a year, count, original employer name, corresponding official
HTTPS source URL and timezone-aware observation date. Filings are not visa approvals
or advertised vacancies. E-Verify history does not guarantee current enrollment of
a particular hiring location; see the [official search caveats](https://www.e-verify.gov/sites/default/files/everify/E-VerifyEmployerSearchToolCaveats.pdf).

Obtain exports from [DOL disclosure data](https://www.dol.gov/agencies/eta/foreign-labor/performance)
or the official USCIS H-1B employer data hub. Export the required sheet to UTF-8 CSV
when a government download is XLSX. Review a legal-name mapping such as
`{"EXACT LEGAL EMPLOYER NAME": "Existing JobPing company name"}`. Unmapped entities
are excluded. Never map a subsidiary to its parent simply because their brands
are related.

```text
poetry run python -m app.cli prepare-employer-history private/lca.csv private/employer-aliases.json private/history.json dol https://www.dol.gov/agencies/eta/foreign-labor/performance --year 2026
poetry run python -m app.cli import-employer-history private/history.json --dry-run
poetry run python -m app.cli import-employer-history private/history.json
```

DOL CSV headers: `CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME`. Only certified
H-1B cases count; duplicate case numbers count once. USCIS CSV headers:
`FISCAL_YEAR,EMPLOYER_NAME,INITIAL_APPROVAL,CONTINUING_APPROVAL`. Initial and
continuing approvals sum within the supplied year. Validate the export's reporting
year before preparing DOL data; do not combine years in one import.
ICE/E-Verify observations use reviewed JSON instead of the CSV converter:

```json
{
  "records": [{
    "company": "Existing JobPing company name",
    "record": {
      "kind": "h1b_filings",
      "year": 2026,
      "count": 1,
      "employer_name": "EXACT LEGAL EMPLOYER NAME",
      "source_url": "https://www.dol.gov/agencies/eta/foreign-labor/performance",
      "observed_at": "2026-10-08T00:00:00Z"
    }
  }]
}
```

This is a schema illustration; the count is not a verified employer record.
Imports replace the same kind/year/legal-name record idempotently, ignore stale
replacements, and reject a file atomically if any mapped company does not exist.
Imports create no discovery events or email matches. Reload the feed after an
import; employer-only imports do not broadcast job events. No government records
are shipped as verified seed data.

## API and rollout

Apply `poetry run alembic upgrade head` before starting updated API/ingestion
processes. Revision `a7b83c96c247` adds `companies.immigration_records`, posting
`details` and occurrence `details`. Existing rows stay unknown without notification
events. Ingestion refreshes evidence on the next poll, including lifecycle `NO_OP`;
there is no Redis identity reset. Historical occurrences retain their evidence
when a confirmed repost starts with its own details. Ambiguous open observations
cannot replace a confirmed closed snapshot. Metadata updates publish only after
SQL commit, independently of durable new/repost notification events.

Example full-feed API filters:

```text
/api/v1/jobs?active=true&policy=opt&policy_value=allowed
/api/v1/jobs?active=true&h1b_history=true
/api/v1/jobs?salary_reported=true&minimum_pay=35&pay_currency=USD&pay_interval=hour
```

Counts and pagination use the same SQL filters. A minimum selects ranges whose
advertised **lower bound** meets the threshold, in the chosen currency and period.
Unknown periods or absent lower bounds do not match. Existing HTTP/live clients
remain compatible with optional metadata. The UI stores shareable feed filters
in the URL; it does not store user visa facts or send these new filters to analytics.

For an initial full rescan, use the established suppression flag/environment
(`NOTIFICATIONS_SUPPRESS_DISCOVERY=true`) to avoid seeding discovery emails.
Deployment, live history imports and live database rescans are separate operator
actions; automated tests use isolated SQLite/PostgreSQL and mocked sources.
