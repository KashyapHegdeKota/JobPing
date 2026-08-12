"""Shared asynchronous interface and execution boundary for job scrapers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Self, cast

import httpx

from app.schemas.job import RawJobPayload


class ScraperError(RuntimeError):
    """Base exception for failures at a scraper boundary."""


class ScraperRequestError(ScraperError):
    """A scraper HTTP request failed before a usable response was returned."""


@dataclass(frozen=True, slots=True)
class ScraperRunStats:
    """Outcome and elapsed wall-clock time for the latest completed run."""

    elapsed_seconds: float
    jobs_count: int
    succeeded: bool


class BaseScraper(ABC):
    """Standard interface and lifecycle for asynchronous source scrapers.

    Subclasses implement :meth:`fetch_jobs`. Callers may use :meth:`run` to add
    consistent timing, outcome logging, and failure accounting around that work.
    """

    def __init__(
        self,
        *,
        scraper_name: str,
        company: str,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.scraper_name = self._require_identity(scraper_name, "scraper_name")
        self.company = self._require_identity(company, "company")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self._logger = logger or logging.getLogger(f"{__name__}.{self.scraper_name}")
        self._closed = False
        self._last_run: ScraperRunStats | None = None
        self._success_count = 0
        self._failure_count = 0

    @abstractmethod
    async def fetch_jobs(self) -> list[RawJobPayload]:
        """Fetch and map all currently visible jobs from the source."""

    @property
    def last_run(self) -> ScraperRunStats | None:
        """Return metrics for the latest finished (non-cancelled) run."""
        return self._last_run

    @property
    def success_count(self) -> int:
        """Return the number of successful runs for this instance."""
        return self._success_count

    @property
    def failure_count(self) -> int:
        """Return the number of failed runs for this instance."""
        return self._failure_count

    async def run(self) -> list[RawJobPayload]:
        """Execute ``fetch_jobs`` with consistent metrics and structured logs."""
        self._ensure_open()
        started = perf_counter()
        self._logger.info("scraper.fetch.started", extra=self._log_context())
        try:
            jobs = await self.fetch_jobs()
        except asyncio.CancelledError:
            elapsed = perf_counter() - started
            self._logger.info(
                "scraper.fetch.cancelled",
                extra=self._log_context(elapsed_seconds=elapsed),
            )
            raise
        except Exception:
            elapsed = perf_counter() - started
            self._failure_count += 1
            self._last_run = ScraperRunStats(elapsed, 0, False)
            self._logger.exception(
                "scraper.fetch.failed",
                extra=self._log_context(elapsed_seconds=elapsed, jobs_count=0),
            )
            raise

        elapsed = perf_counter() - started
        self._success_count += 1
        self._last_run = ScraperRunStats(elapsed, len(jobs), True)
        self._logger.info(
            "scraper.fetch.succeeded",
            extra=self._log_context(elapsed_seconds=elapsed, jobs_count=len(jobs)),
        )
        return jobs

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Send one HTTP request and translate transport/status failures."""
        self._ensure_open()
        kwargs.setdefault("timeout", self._timeout)
        try:
            response = await self._client.request(method, url, **cast(Any, kwargs))
            response.raise_for_status()
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            raise ScraperRequestError(
                f"{self.scraper_name} request failed: {method.upper()} {url}"
            ) from exc
        return response

    async def aclose(self) -> None:
        """Close only the HTTP client created by this scraper."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ScraperError(f"{self.scraper_name} scraper is closed")

    def _log_context(self, **values: object) -> dict[str, object]:
        return {
            "scraper_name": self.scraper_name,
            "company": self.company,
            **values,
        }

    @staticmethod
    def _require_identity(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} must not be empty")
        return normalized


__all__ = [
    "BaseScraper",
    "ScraperError",
    "ScraperRequestError",
    "ScraperRunStats",
]
