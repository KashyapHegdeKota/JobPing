"""Async ingestion client for ApplyGuy's machine-readable GitHub feeds."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlparse

import httpx

from app.schemas.job import JobType, RawJobPayload
from app.scrapers.base import BaseScraper, ScraperError
from app.utils.resilience import external_retry

_LOGGER = logging.getLogger(__name__)


class ApplyGuyFeed(StrEnum):
    """The two supported ApplyGuy repositories and their fixed JobPing type."""

    INTERNSHIPS = "internship"
    NEW_GRAD = "new_grad"

    @property
    def repository(self) -> str:
        return "2027-Internships" if self is ApplyGuyFeed.INTERNSHIPS else "2027-New-Grad-Jobs"

    @property
    def path(self) -> str:
        return (
            "data/internships.json"
            if self is ApplyGuyFeed.INTERNSHIPS
            else "data/new-grad-jobs.json"
        )

    @property
    def source(self) -> str:
        return "applyguy_internships" if self is ApplyGuyFeed.INTERNSHIPS else "applyguy_new_grad"

    @property
    def job_type(self) -> JobType:
        return JobType.INTERNSHIP if self is ApplyGuyFeed.INTERNSHIPS else JobType.NEW_GRAD

    @property
    def raw_url(self) -> str:
        return f"https://raw.githubusercontent.com/ApplyGuy/{self.repository}/main/{self.path}"

    @classmethod
    def parse(cls, value: str | ApplyGuyFeed) -> ApplyGuyFeed:
        """Accept CLI-friendly singular/plural and hyphenated feed names."""
        if isinstance(value, cls):
            return value
        normalized = value.strip().casefold().replace("-", "_")
        aliases = {
            "intern": cls.INTERNSHIPS,
            "internship": cls.INTERNSHIPS,
            "internships": cls.INTERNSHIPS,
            "newgrad": cls.NEW_GRAD,
            "new_grad": cls.NEW_GRAD,
            "new_grads": cls.NEW_GRAD,
            "new_grad_jobs": cls.NEW_GRAD,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError("type must be internship, new-grad, or all") from exc


class ApplyGuyError(ScraperError):
    """The ApplyGuy feed was unavailable or had an invalid top-level shape."""


class ApplyGuyResponseError(ApplyGuyError):
    """ApplyGuy returned a response that could not be consumed as a feed."""


class ApplyGuyScraper(BaseScraper):
    """Fetch one ApplyGuy JSON feed and map valid rows to raw JobPing payloads."""

    def __init__(
        self,
        feed_type: str | ApplyGuyFeed = ApplyGuyFeed.INTERNSHIPS,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: httpx.Timeout | float = 10.0,
        feed_url: str | None = None,
    ) -> None:
        feed = ApplyGuyFeed.parse(feed_type)
        super().__init__(
            scraper_name=feed.source,
            company="ApplyGuy",
            client=client,
            timeout=timeout,
        )
        self.feed = feed
        self.feed_url = (feed_url or feed.raw_url).strip()
        if not _is_http_url(self.feed_url):
            raise ValueError("feed_url must be an absolute HTTP(S) URL")
        self.rejected_count = 0
        self.rejected_rows: list[tuple[str | None, str]] = []

    @property
    def source(self) -> str:
        """Stable source identifier used by metrics and payloads."""
        return self.feed.source

    @property
    def job_type(self) -> JobType:
        return self.feed.job_type

    async def fetch_jobs(self) -> list[RawJobPayload]:
        """Fetch, validate, and map the feed while isolating malformed rows."""
        self.rejected_count = 0
        self.rejected_rows = []
        try:
            response = await self._request_feed()
        except httpx.HTTPStatusError as exc:
            raise ApplyGuyError(
                f"ApplyGuy feed request failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ApplyGuyError("ApplyGuy feed request failed") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ApplyGuyResponseError("ApplyGuy feed returned invalid JSON") from exc
        if not isinstance(body, Mapping) or not isinstance(body.get("jobs"), list):
            raise ApplyGuyResponseError("ApplyGuy feed must contain a jobs list")

        updated_at = _parse_datetime(body.get("updatedAt"))
        jobs: list[RawJobPayload] = []
        for index, item in enumerate(body["jobs"]):
            try:
                mapped = self._map_job(item, updated_at=updated_at)
            except (TypeError, ValueError, KeyError) as exc:
                self.rejected_count += 1
                self.rejected_rows.append((_source_id(item), str(exc)))
                _LOGGER.warning(
                    "applyguy.job.rejected",
                    extra={"source": self.source, "row_index": index, "reason": str(exc)},
                )
                continue
            if mapped is None:
                self.rejected_count += 1
                self.rejected_rows.append(
                    (_source_id(item), "missing required job fields or valid URL")
                )
                _LOGGER.warning(
                    "applyguy.job.rejected",
                    extra={
                        "source": self.source,
                        "row_index": index,
                        "reason": "missing required job fields or valid URL",
                    },
                )
                continue
            jobs.append(mapped)
        return jobs

    @external_retry
    async def _request_feed(self) -> httpx.Response:
        self._ensure_open()
        response = await self._client.get(
            self.feed_url,
            timeout=self._timeout,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response

    def _map_job(self, item: object, *, updated_at: datetime | None) -> RawJobPayload | None:
        if not isinstance(item, Mapping):
            return None
        source_id = _text(item.get("id"))
        company = _text(item.get("company"))
        title = _text(item.get("title"))
        apply_url = _select_apply_url(item)
        if not source_id or not company or not title or apply_url is None:
            return None

        metadata = {
            "category": item.get("category"),
            "season": item.get("season"),
            "posted": item.get("posted"),
            "age": item.get("age"),
            "eligibility": item.get("eligibility"),
            "matchKind": item.get("matchKind"),
            "applyguy_url": _text(item.get("url")),
            "listing_url": _text(item.get("listingUrl")),
            "updated_at": updated_at.isoformat() if updated_at is not None else None,
            "repository": self.feed.repository,
            "path": self.feed.path,
        }
        return RawJobPayload(
            source=self.source,
            source_id=source_id,
            company=company,
            title=title,
            apply_url=apply_url,
            location=_text(item.get("location")),
            season=2027,
            job_type=self.job_type.value,
            is_closed=False,
            observed_at=updated_at,
            payload=metadata,
        )


def _select_apply_url(item: Mapping[object, object]) -> str | None:
    """Prefer a valid direct listing URL over ApplyGuy's tracking URL."""
    listing_url = _text(item.get("listingUrl"))
    if _is_http_url(listing_url):
        return listing_url
    applyguy_url = _text(item.get("url"))
    return applyguy_url if _is_http_url(applyguy_url) else None


def _source_id(item: object) -> str | None:
    return _text(item.get("id")) if isinstance(item, Mapping) else None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).strip()
    return normalized or None


def _is_http_url(value: str | None) -> bool:
    if value is None:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


__all__ = [
    "ApplyGuyError",
    "ApplyGuyFeed",
    "ApplyGuyResponseError",
    "ApplyGuyScraper",
]
