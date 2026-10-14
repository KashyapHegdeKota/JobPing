# Sponsorship/pay release checkpoint

Implementation is complete in isolated `codex/sponsorship-pay-screening` release
worktrees. Publication and production activation are authorized by the user.
See `pre-application-evidence.md` for architecture and operator commands.

Validated before latest-base release integration: 978 backend tests, 85.25% coverage,
17 browser tests, 11 isolated PostgreSQL tests; UI 150 tests, lint/types and enabled
build. Controlled desktop/390px mobile screenshots passed visual/overflow review.
Final rules restrict evidence to exact-role content, retain conflicting/conditional
restrictions, require attached pay units/support spans, and fence occurrence/title/
URL changes. SQL parity, account isolation, refresh concurrency and rollback are covered.

Fifty-role historical review yielded eight supported pay matches, 34 review outcomes
and eight retrieval failures. Strict fresh collection yielded eight supported Figma
pay matches, four completed review outcomes and 38 retrieval/scope failures. All eight
positives were checked against source quotes; no unsupported positive was found.
Unknown evidence remains review; coverage is limited and never implies eligibility.

Native ARM64 renderer acceptance passed browser startup, public pinned-IP HTTPS,
private destination rejection and read-only scoped extraction. The VM's missing
Docker-owned chains were restored without daemon/service restarts or firewall flushes;
Docker then created the dedicated `jobping-evidence` network. Firewall backup is
`/var/backups/jobping/evidence-network-before-20261014.rules`. Existing services stayed
running. Production renderer must be rebuilt from final committed source and pinned.

Remaining: finish latest-base checks/controller simulation, publish both PRs, pass CI,
verify a fresh backup, deploy through existing blue-green pipeline, start immutable
renderer/evidence worker, verify queue/source/API health, then activate the frontend
flag and deploy/verify Vercel. Remember-me is already live (UI PR #26).

Do not publish original dirty `.env.example`, candidate/application example JSON,
private snapshots, temporary helpers or UI `.env.local`. No tests use normal developer
or production databases. Flags remain off in committed examples. Preserve notification
opt-ins and shared PostgreSQL/Redis; never downgrade the schema on API rollback.
