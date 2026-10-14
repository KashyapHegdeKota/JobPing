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

Production rollout completed October 14: backend PR #45 merged as `7e69249`,
all CI/native-image/deployment gates passed, migration applied and three workers are
healthy. UI PR #27 merged as `cb4966a`; screening-enabled Vercel production deployment
`ETxS2SKEY996emR41YH57oL1Vbwo` is Ready on jobping.website. Signed-in profile/feed/
dialog and desktop/390px phone review passed with no warnings/errors or overflow.
No user requirements or notification settings were changed. Live API/evidence/auth/
CORS/WebSocket/SSE checks passed. Backfill has persisted complete evidence checks;
unsupported sources remain review. See the rollout record in pre-application-evidence.md.
Remember-me remains live (UI PR #26). Latest-base tests: 984 passed, 84.80% coverage;
27 Linux controller simulations passed. CI's stale Windows Python cache was evicted;
the rerun passed, and PR #46 pins cache keys to the exact installed Python version.

Do not publish original dirty `.env.example`, candidate/application example JSON,
private snapshots, temporary helpers or UI `.env.local`. No tests use normal developer
or production databases. Flags remain off in committed examples. Preserve notification
opt-ins and shared PostgreSQL/Redis; never downgrade the schema on API rollback.
