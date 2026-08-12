"""Credential-safe proxy loading, rotation, and health tracking."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from time import monotonic
from urllib.parse import unquote, urlsplit

import httpx

_ALLOWED_SCHEMES = {"http", "https", "socks5"}


class ProxyError(RuntimeError):
    """Base proxy configuration/provider error."""


class NoProxyAvailableError(ProxyError):
    """Every configured proxy is unavailable or cooling down."""


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    """Validated endpoint with optional decoded credentials."""

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, value: str) -> ProxyEndpoint:
        raw = value.strip()
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("invalid proxy URL") from exc
        if parsed.scheme.casefold() not in _ALLOWED_SCHEMES or not parsed.hostname or port is None:
            raise ValueError("proxy requires http, https, or socks5 scheme, host, and port")
        if (
            not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid proxy URL")
        return cls(
            parsed.scheme.casefold(),
            parsed.hostname,
            port,
            unquote(parsed.username) if parsed.username is not None else None,
            unquote(parsed.password) if parsed.password is not None else None,
        )

    @property
    def redacted(self) -> str:
        auth = "***@" if self.username is not None or self.password is not None else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def playwright(self) -> dict[str, str]:
        result = {"server": f"{self.scheme}://{self.host}:{self.port}"}
        if self.username is not None:
            result["username"] = self.username
        if self.password is not None:
            result["password"] = self.password
        return result

    def __str__(self) -> str:
        return self.redacted


@dataclass(slots=True)
class ProxyHealth:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    dead: bool = False


class ProxyManager:
    """Concurrency-safe deterministic round-robin proxy pool."""

    def __init__(
        self,
        proxies: Iterable[str | ProxyEndpoint] | None = None,
        *,
        clock: Callable[[], float] = monotonic,
        base_cooldown: float = 30.0,
        max_cooldown: float = 900.0,
        max_failures: int = 5,
    ) -> None:
        if base_cooldown <= 0 or max_cooldown < base_cooldown or max_failures < 1:
            raise ValueError("invalid proxy health configuration")
        configured = proxies if proxies is not None else self._split(os.getenv("PROXY_LIST", ""))
        endpoints = [
            item if isinstance(item, ProxyEndpoint) else ProxyEndpoint.parse(item)
            for item in configured
        ]
        self._endpoints = tuple(dict.fromkeys(endpoints))
        self._health = {endpoint: ProxyHealth() for endpoint in self._endpoints}
        self._clock = clock
        self._base_cooldown = base_cooldown
        self._max_cooldown = max_cooldown
        self._max_failures = max_failures
        self._cursor = 0
        self._lock = asyncio.Lock()

    @classmethod
    async def from_provider(
        cls,
        url: str,
        *,
        client: httpx.AsyncClient,
        **kwargs: object,
    ) -> ProxyManager:
        """Load newline text, JSON array, or ``{"proxies": [...]}`` provider data."""
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProxyError("proxy provider request failed") from exc
        content_type = response.headers.get("content-type", "").casefold()
        try:
            if "json" in content_type:
                payload = response.json()
                values = payload.get("proxies") if isinstance(payload, Mapping) else payload
                if not isinstance(values, list) or not all(
                    isinstance(item, str) for item in values
                ):
                    raise ValueError
            else:
                values = cls._split(response.text)
            return cls(values, **kwargs)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProxyError("proxy provider returned an invalid payload") from exc

    async def acquire(self) -> ProxyEndpoint:
        async with self._lock:
            now = self._clock()
            total = len(self._endpoints)
            for offset in range(total):
                index = (self._cursor + offset) % total
                endpoint = self._endpoints[index]
                health = self._health[endpoint]
                if health.dead and now >= health.cooldown_until:
                    health.dead = False
                    health.consecutive_failures = 0
                if not health.dead and now >= health.cooldown_until:
                    self._cursor = (index + 1) % total
                    return endpoint
            raise NoProxyAvailableError("no proxy is currently available")

    async def report_success(self, endpoint: ProxyEndpoint) -> None:
        async with self._lock:
            health = self._known(endpoint)
            health.successes += 1
            health.consecutive_failures = 0
            health.cooldown_until = 0.0
            health.dead = False

    async def report_failure(self, endpoint: ProxyEndpoint, status_code: int | None = None) -> None:
        async with self._lock:
            health = self._known(endpoint)
            health.failures += 1
            health.consecutive_failures += 1
            if status_code in {403, 429, 503}:
                delay = min(
                    self._max_cooldown,
                    self._base_cooldown * (2 ** (health.consecutive_failures - 1)),
                )
                health.cooldown_until = self._clock() + delay
                health.dead = health.consecutive_failures >= self._max_failures

    def health(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        current = self._known(endpoint)
        return ProxyHealth(
            current.successes,
            current.failures,
            current.consecutive_failures,
            current.cooldown_until,
            current.dead,
        )

    def _known(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        try:
            return self._health[endpoint]
        except KeyError as exc:
            raise ValueError("proxy endpoint is not managed by this pool") from exc

    @staticmethod
    def _split(value: str) -> list[str]:
        return [
            item.strip() for line in value.splitlines() for item in line.split(",") if item.strip()
        ]


__all__ = [
    "NoProxyAvailableError",
    "ProxyEndpoint",
    "ProxyError",
    "ProxyHealth",
    "ProxyManager",
]
