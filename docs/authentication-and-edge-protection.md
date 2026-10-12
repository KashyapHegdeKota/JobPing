# Authentication and edge activation

Application code and proxy templates are implemented. Provider/hosting/DNS/firewall
changes remain an operational rollout; no such account changes were made here.

## Firebase authentication

JobPing uses Firebase's browser SDK directly for email/password and Google popup
sign-in. The implemented dialog prevents duplicate in-flight actions, displays
generic account-safe errors and reacts to `auth/too-many-requests` with a one-minute
local cooldown. The SDK/provider remains authoritative; the local countdown is a
minimum wait suggestion and never guarantees that Firebase has lifted its block.
Credentials and OAuth popups are never automatically retried. Refreshing the page
cannot bypass server API quotas, and the supplementary browser cooldown survives
reloads within the tab. Ordinary SDK token refresh is not an interactive login.

For stronger email/password protection, inspect the project's Identity Platform
availability, billing, authorized domains and current authentication config. Follow
the official [reCAPTCHA integration guide](https://docs.cloud.google.com/identity-platform/docs/recaptcha-enterprise):
enable the required API, configure email/password bot protection in AUDIT, verify
supported clients and score/false-positive metrics, then enable ENFORCE using the
reviewed risk threshold. Google popup authentication is a separate provider flow;
email/password reCAPTCHA must not be advertised as covering it. Review production
and deliberately supported previews without enabling arbitrary origins.

Validate email sign-in/sign-up, Google sign-in, cancelled popups, provider throttles
and email verification using controlled test accounts. Never log passwords, tokens,
CAPTCHA tokens or full provider responses. Do not create a nominal JobPing login
preflight endpoint: direct Firebase requests would bypass it. Exact custom attempt
quotas would need a separately designed enforced authentication boundary. See
[Firebase's provider limits](https://firebase.google.com/docs/auth/limits).

## Vercel pages

Inspect the deployed project's firewall plan/rule allowance and costs, then create
a navigation rate rule in Log mode. The architecture proposes observing around
60 navigation requests/minute/IP initially, subject to real shared-network traffic.
Exclude static assets and distinguish navigation/framework prefetch traffic.
Validate full reloads and navigation on desktop/mobile and several tabs before
enforcing. Keep challenges on appropriate browser page traffic; never serve an
interactive challenge to Resend webhooks or raw API/stream clients.

Vercel's default DDoS mitigation protects Vercel deployments, while custom rate
counters are regional. It does not proxy the separately hosted Oracle API simply
because the frontend uses that API. See [Vercel rate limiting](https://vercel.com/docs/vercel-firewall/vercel-waf/rate-limiting)
and [DDoS mitigation](https://vercel.com/docs/vercel-firewall/ddos-mitigation).

## Oracle API upstream boundary

Confirm current authoritative DNS/proxy configuration before adding an upstream
DDoS service. An option is a Cloudflare proxy or tunnel for `api.jobping.website`.
Validate TLS, unbuffered SSE, WebSocket upgrade/keepalive, provider callbacks and
CORS. Do not cache private responses, 429/503 responses or event streams.

Prevent direct-origin bypass through a tunnel or carefully scoped origin firewall
and origin authentication. The VM hosts other applications: changes must be scoped
to JobPing and retain SSH/Certbot renewal and unrelated sites. If proxy forwarding
headers identify users, configure `real_ip_header` and `set_real_ip_from` for only
that proxy's trusted ranges. Nginx must then overwrite the forwarding headers sent
to loopback Uvicorn. Never trust arbitrary public X-Forwarded-For or CF-Connecting-IP
headers. Verify direct-origin access is denied and forged addresses cannot mint
fresh quotas. See [origin protection](https://developers.cloudflare.com/fundamentals/security/protect-your-origin-server/).

Account configuration, rules, origin routing and production activation must be
validated during deployment. API rate limits alone do not absorb bandwidth-level
DDoS attacks. Keep a rollback record for each independently activated layer.
