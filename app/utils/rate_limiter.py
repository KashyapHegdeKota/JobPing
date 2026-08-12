"""Domain-scoped rate limiting and resilient user-agent rotation."""

from __future__ import annotations

import asyncio
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Token generation rate and maximum burst capacity."""

    rate: float
    capacity: float

    def __post_init__(self) -> None:
        if self.rate <= 0 or self.capacity < 1:
            raise ValueError("rate must be positive and capacity must be at least one")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    lock: asyncio.Lock


class DomainRateLimiter:
    """Concurrency-safe token buckets independently scoped by normalized domain."""

    def __init__(
        self,
        default: RateLimit,
        *,
        domains: Mapping[str, RateLimit] | None = None,
        max_domains: int = 256,
        clock: Clock = monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if max_domains < 1:
            raise ValueError("max_domains must be positive")
        self.default = default
        self.domains = {_normalize_domain(key): value for key, value in (domains or {}).items()}
        self.max_domains = max_domains
        self._clock = clock
        self._sleep = sleep
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._state_lock = asyncio.Lock()

    async def acquire(self, url_or_domain: str, *, tokens: float = 1.0) -> None:
        """Wait until tokens are available without holding a lock while sleeping."""
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        domain = _normalize_domain(url_or_domain)
        limit = self.domains.get(domain, self.default)
        if tokens > limit.capacity:
            raise ValueError("tokens cannot exceed bucket capacity")
        bucket = await self._bucket(domain, limit)
        while True:
            async with bucket.lock:
                now = self._clock()
                elapsed = max(0.0, now - bucket.updated_at)
                bucket.tokens = min(limit.capacity, bucket.tokens + elapsed * limit.rate)
                bucket.updated_at = now
                if bucket.tokens >= tokens:
                    bucket.tokens -= tokens
                    return
                delay = (tokens - bucket.tokens) / limit.rate
            await self._sleep(delay)

    async def _bucket(self, domain: str, limit: RateLimit) -> _Bucket:
        async with self._state_lock:
            if bucket := self._buckets.get(domain):
                self._buckets.move_to_end(domain)
                return bucket
            if len(self._buckets) >= self.max_domains:
                self._buckets.popitem(last=False)
            bucket = _Bucket(limit.capacity, self._clock(), asyncio.Lock())
            self._buckets[domain] = bucket
            return bucket


def _normalize_domain(value: str) -> str:
    candidate = value.strip().lower()
    if not candidate:
        raise ValueError("domain must not be empty")
    parsed = urlsplit(candidate if "://" in candidate else f"//{candidate}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("invalid domain or URL")
    return hostname.rstrip(".")


_FALLBACK_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 Version/18 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
)


class UserAgentRotator:
    """Select user agents dynamically with an always-available static fallback."""

    def __init__(
        self,
        *,
        provider: Callable[[], str] | None = None,
        rng: random.Random | None = None,
        fallback: Sequence[str] = _FALLBACK_AGENTS,
        browsers: Sequence[str] | None = None,
        platforms: Sequence[str] | None = None,
    ) -> None:
        if not fallback:
            raise ValueError("fallback must not be empty")
        self._rng = rng or random.Random()
        self._fallback = tuple(fallback)
        self._provider = provider or self._fake_provider(browsers, platforms)

    def get(self) -> str:
        """Return a provider value, falling back on any provider failure."""
        try:
            value = self._provider().strip()
            if value:
                return value
        except Exception:
            pass
        return self._rng.choice(self._fallback)

    def headers(self, headers: Mapping[str, str] | None = None) -> dict[str, str]:
        """Return copied headers, preserving any caller-supplied User-Agent."""
        result = dict(headers or {})
        if not any(key.casefold() == "user-agent" for key in result):
            result["User-Agent"] = self.get()
        return result

    @staticmethod
    def _fake_provider(
        browsers: Sequence[str] | None, platforms: Sequence[str] | None
    ) -> Callable[[], str]:
        def provide() -> str:
            from fake_useragent import UserAgent

            options: dict[str, object] = {}
            if browsers:
                options["browsers"] = list(browsers)
            if platforms:
                options["platforms"] = list(platforms)
            return str(UserAgent(**options).random)

        return provide


__all__ = ["DomainRateLimiter", "RateLimit", "UserAgentRotator"]
