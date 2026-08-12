"""Concurrency-safe in-process scraper metrics and health diagnostics."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Self


@dataclass(frozen=True, slots=True)
class ScraperHealth:
    """Immutable aggregate health snapshot for one scraper."""

    scraper: str
    runs: int
    successes: int
    failures: int
    jobs_parsed: int
    average_duration_seconds: float
    error_rate: float
    last_error: str | None
    last_run_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProxyHealth:
    """Aggregate proxy outcomes without exposing proxy credentials."""

    proxy: str
    successes: int
    failures: int
    failure_rate: float
    last_status: int | None


@dataclass(slots=True)
class _ScraperCounters:
    runs: int = 0
    successes: int = 0
    failures: int = 0
    jobs: int = 0
    total_seconds: float = 0.0
    last_error: str | None = None
    last_run_at: datetime | None = None


@dataclass(slots=True)
class _ProxyCounters:
    successes: int = 0
    failures: int = 0
    last_status: int | None = None


class ScraperMetrics:
    """Record bounded operational metrics for scraper and proxy activity."""

    def __init__(self, *, recent_errors_limit: int = 100) -> None:
        if recent_errors_limit <= 0:
            raise ValueError("recent_errors_limit must be positive")
        self._scrapers: defaultdict[str, _ScraperCounters] = defaultdict(_ScraperCounters)
        self._proxies: defaultdict[str, _ProxyCounters] = defaultdict(_ProxyCounters)
        self._recent_errors: deque[tuple[datetime, str, str]] = deque(maxlen=recent_errors_limit)
        self._lock = asyncio.Lock()

    def timer(self, scraper: str) -> ScraperTimer:
        """Return an async timing context for a named scraper run."""
        return ScraperTimer(self, self._identity(scraper))

    async def record_run(
        self,
        scraper: str,
        *,
        duration_seconds: float,
        jobs_parsed: int,
        error: BaseException | None = None,
    ) -> None:
        """Record one completed scraper run."""
        name = self._identity(scraper)
        if duration_seconds < 0 or jobs_parsed < 0:
            raise ValueError("duration_seconds and jobs_parsed must be non-negative")
        async with self._lock:
            counters = self._scrapers[name]
            counters.runs += 1
            counters.total_seconds += duration_seconds
            counters.jobs += jobs_parsed
            counters.last_run_at = datetime.now(UTC)
            if error is None:
                counters.successes += 1
            else:
                counters.failures += 1
                message = f"{type(error).__name__}: {error}"
                counters.last_error = message
                self._recent_errors.append((counters.last_run_at, name, message))

    async def record_proxy(self, proxy: str, *, success: bool, status: int | None = None) -> None:
        """Record one redacted proxy outcome."""
        identity = self._redact_proxy(proxy)
        async with self._lock:
            counters = self._proxies[identity]
            if success:
                counters.successes += 1
            else:
                counters.failures += 1
            counters.last_status = status

    async def scraper_health(self) -> tuple[ScraperHealth, ...]:
        """Return stable scraper snapshots sorted by name."""
        async with self._lock:
            return tuple(
                ScraperHealth(
                    scraper=name,
                    runs=value.runs,
                    successes=value.successes,
                    failures=value.failures,
                    jobs_parsed=value.jobs,
                    average_duration_seconds=(
                        value.total_seconds / value.runs if value.runs else 0.0
                    ),
                    error_rate=value.failures / value.runs if value.runs else 0.0,
                    last_error=value.last_error,
                    last_run_at=value.last_run_at,
                )
                for name, value in sorted(self._scrapers.items())
            )

    async def proxy_health(self) -> tuple[ProxyHealth, ...]:
        """Return stable proxy snapshots sorted by redacted identity."""
        async with self._lock:
            return tuple(
                ProxyHealth(
                    proxy=name,
                    successes=value.successes,
                    failures=value.failures,
                    failure_rate=(
                        value.failures / (value.successes + value.failures)
                        if value.successes + value.failures
                        else 0.0
                    ),
                    last_status=value.last_status,
                )
                for name, value in sorted(self._proxies.items())
            )

    async def recent_errors(self) -> tuple[tuple[datetime, str, str], ...]:
        """Return the bounded chronological error history."""
        async with self._lock:
            return tuple(self._recent_errors)

    @staticmethod
    def _identity(value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("scraper name must be non-empty")
        return normalized

    @staticmethod
    def _redact_proxy(value: str) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(value)
        host = parsed.hostname or "unknown"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))


class ScraperTimer:
    """Async context manager recording elapsed time and failures."""

    def __init__(self, metrics: ScraperMetrics, scraper: str) -> None:
        self._metrics = metrics
        self._scraper = scraper
        self._started = 0.0
        self.jobs_parsed = 0

    async def __aenter__(self) -> Self:
        self._started = monotonic()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self._metrics.record_run(
            self._scraper,
            duration_seconds=monotonic() - self._started,
            jobs_parsed=self.jobs_parsed,
            error=exc_value,
        )


__all__ = ["ProxyHealth", "ScraperHealth", "ScraperMetrics", "ScraperTimer"]
