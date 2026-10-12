"""Reject ingress before routing, verification, body mapping or database sessions."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.request_limits.cache import PublicCache
from app.request_limits.errors import reject
from app.request_limits.identity import client_address
from app.request_limits.leases import StreamLeases
from app.request_limits.limiter import LimiterUnavailable, RequestLimiter
from app.request_limits.policies import family, private_family
from app.request_limits.streams import serve_stream


class RequestLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.active_http = 0
        self.cache: PublicCache | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        limiter: RequestLimiter | None = getattr(scope["app"].state, "request_limiter", None)
        if limiter is None:
            settings = getattr(scope["app"].state, "request_limit_settings", None)
            if settings and settings.mode != "off":
                await reject(scope, receive, send, 503)
            else:
                await self.app(scope, receive, send)
            return
        if limiter.settings.mode == "off":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        operation = family(path, scope.get("method", "GET"))
        address = client_address(scope)
        if (
            path == "/api/v1/health"
            and scope.get("method") == "GET"
            and (scope.get("client") or ("", 0))[0] in {"127.0.0.1", "::1"}
        ):
            await self.app(scope, receive, send)
            return
        try:
            for policy, kind, subject in [
                ("aggregate", "global", "api"),
                ("ingress", "ip", address),
            ]:
                decision = await limiter.check(policy, kind, subject)
                if not decision.allowed:
                    await reject(scope, receive, send, 429, decision.retry_after)
                    return
            if not private_family(operation):
                decision = await limiter.check(operation, "ip", address)
                if not decision.allowed:
                    await reject(scope, receive, send, 429, decision.retry_after)
                    return
        except LimiterUnavailable:
            # No unlimited fallback: proxy-dependent fail-open is deliberately deferred.
            await reject(scope, receive, send, 503)
            return

        if operation == "connect":
            leases = StreamLeases(limiter)
            try:
                lease = await leases.acquire(address)
            except LimiterUnavailable:
                if limiter.settings.mode == "shadow":
                    await self.app(scope, receive, send)
                else:
                    await reject(scope, receive, send, 503)
                return
            if lease is None:
                await reject(scope, receive, send, 429, limiter.settings.lease_seconds)
                return
            await serve_stream(self.app, scope, receive, send, leases, lease)
            return

        # One API worker is the existing deployment contract. Reject immediately,
        # rather than queueing thousands of requests behind database concurrency.
        if self.active_http >= 32 and limiter.settings.mode == "enforce":
            await reject(scope, receive, send, 503)
            return
        self.active_http += 1
        try:
            await self.cached_request(limiter, scope, receive, send)
        finally:
            self.active_http -= 1

    async def cached_request(
        self, limiter: RequestLimiter, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if self.cache is None:
            self.cache = PublicCache(limiter.settings.cache_seconds)
        eligible = (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and scope["path"] in {"/api/v1/companies", "/api/v1/stats"}
            and not scope.get("query_string")
            and not any(
                name.lower() in {b"authorization", b"cookie"} for name, _ in scope["headers"]
            )
        )
        if not eligible:
            await self.app(scope, receive, send)
            return
        cached = self.cache.get(scope["path"])
        if cached:
            await send({"type": "http.response.start", "status": 200, "headers": cached.headers})
            await send({"type": "http.response.body", "body": cached.body})
            return
        start: Message | None = None

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            if message["type"] == "http.response.body" and start and start["status"] == 200:
                if not message.get("more_body", False):
                    self.cache.put(scope["path"], start["headers"], message.get("body", b""))
                else:
                    start = None
            await send(message)

        await self.app(scope, receive, capture)
