# Selected company coverage

Checked October 8, 2026. The direct ATS registry supports verified public
Greenhouse and global Lever boards. It does not make every careers portal a
Greenhouse/Lever source. The table records configured coverage, not a claim that
a corporate group has only one recruiting system worldwide.

Use `examples/ats_sources.tech_companies.json` as a starting registry for the
requested companies. It configures Palantir, two SK hynix America boards, and
Alphabet subsidiary Waymo. Employer names remain distinct from their board tokens
and the Alphabet parent name. The two SK
subsidiaries retain their own employer names: SK hynix America and SK hynix memory
solutions America Inc. Palantir, Memory Solutions America and Waymo names were
cross-checked against the current ApplyGuy feeds. Before ingestion, ensure these
names also match any differently named companies in your existing database;
there is no fuzzy company-name aliasing.

| Requested company | Direct coverage configured in this change | Official careers source / remaining work |
| --- | --- | --- |
| NVIDIA | None verified | [NVIDIA careers](https://jobs.nvidia.com/careers): separate portal integration |
| Apple | None verified | [Apple job search](https://jobs.apple.com/en-us/search): separate portal integration |
| Alphabet | Waymo Greenhouse board only | [Waymo](https://careers.withwaymo.com/jobs/search); [Google job search](https://www.google.com/about/careers/applications/jobs/results) and other subsidiaries remain separate |
| Microsoft | None verified | [Microsoft careers](https://careers.microsoft.com/v2/global/en/home.html): separate portal integration |
| TSMC | None verified | [TSMC Arizona jobs](https://ro.careers.tsmc.com/go/CorporateJobs/4716710/); global portal discovery remains |
| Meta Platforms | None verified | [Meta careers](https://www.metacareers.com/): direct page retrieval failed; existing Meta scraper is not scheduler-wired by this change |
| Broadcom | None verified | [Broadcom careers teams](https://www.broadcom.com/company/careers/teams): separate portal integration |
| SK Hynix | Two America Greenhouse boards | [SK hynix America](https://job-boards.greenhouse.io/skhynixamerica) and [Memory Solutions America](https://job-boards.greenhouse.io/skhynixmemorysolutionsamericainc); not worldwide coverage |
| Micron Technology | None verified | [Micron careers](https://careers.micron.com/careers): separate portal integration |
| Advanced Micro Devices | None verified | [AMD careers](https://careers.amd.com/careers-home/): separate portal integration |
| ASML Holding | None verified | [ASML job search](https://www.asml.com/en/careers/find-your-job): separate portal integration |
| Intel | None verified | [Intel application guidance](https://www.intel.com/content/www/us/en/support/articles/000089205/programs/jobs-at-intel.html) describes Workday; requires board verification and polling integration |
| Cisco Systems | None verified | [Cisco job search](https://careers.cisco.com/global/en/search-results): separate portal integration |
| Palantir Technologies | Lever `palantir` | [Palantir public board](https://jobs.lever.co/palantir) |
| Oracle Corporation | None verified | [Oracle careers](https://www.oracle.com/careers/): separate portal integration |
| Applied Materials | None verified | [Applied Materials job search](https://jobs.appliedmaterials.com/en/search-jobs): separate portal integration |
| Lam Research | None verified | [Lam Research careers](https://careers.lamresearch.com/careers): separate portal integration |
| Dell Technologies | None verified | [Dell careers](https://enterpriseplatform.dell.com/hcmUI/CandidateExperience/en/sites/careers): Oracle-style candidate portal; separate integration |
| Palo Alto Networks | None verified | [Palo Alto Networks careers](https://jobs.paloaltonetworks.com/en/): separate portal integration |
| ARM Holdings | None verified | [Arm job search](https://careers.arm.com/en/search-jobs): separate portal integration |

The public API verification was read-only and used the existing source clients;
it did not ingest jobs, mutate PostgreSQL/Redis, or send notifications. The
observed boards returned 313 Palantir rows, 52 SK hynix America rows, 26 Memory
Solutions America rows and 371 Waymo rows. Counts will change.

With strict 2027 title eligibility, Waymo had 52 eligible rows, while the three
other boards had zero. Palantir had 79 internship/new-grad rows without a year;
Memory Solutions America had five internship rows without a year. To accept
such roles into the configured 2027 season, deliberately set `allow_undated`
to true on those entries. The supplied registry retains the conservative false
setting. It does not infer 2027 from a posting date or from missing title evidence.

Search results still reference a historical DeepMind Greenhouse board, but its
public API returned 404. The [current DeepMind careers page](https://deepmind.google/careers/)
links open roles to Google Careers. It is excluded from the registry.

Validate without contacting sources or writing to services:

```text
poetry run python -m app.cli run-ats-sync --sources-file examples/ats_sources.tech_companies.json --dry-run
poetry run python -m app.cli start-scheduler --sources-file examples/ats_sources.tech_companies.json --dry-run
```

For initial ingestion, set `NOTIFICATIONS_SUPPRESS_DISCOVERY=true` and run the
one-shot command with the configured database and Redis available. Then set
`ATS_SOURCES_FILE` to the edited registry for subsequent scheduler runs.
