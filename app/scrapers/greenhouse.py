"""Direct asynchronous client for the public Greenhouse Job Board API."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Self
from urllib.parse import quote, urlparse

import httpx

from app.schemas.job import RawJobPayload
from app.scrapers.base import BaseScraper

GREENHOUSE_API_URL = "https://boards-api.greenhouse.io/v1/boards"
_WHITESPACE = re.compile(r"\s+")


class GreenhouseError(RuntimeError):
    """Base error for Greenhouse transport and response failures."""


class GreenhouseBoardNotFoundError(GreenhouseError):
    """The requested public Greenhouse board does not exist."""


class GreenhouseRateLimitError(GreenhouseError):
    """Greenhouse throttled the public board request."""


class GreenhouseScraper(BaseScraper):
    """Fetch jobs from one Greenhouse board and map them to ingestion payloads.

    Greenhouse's public jobs endpoint returns the complete active board in one
    response; it does not expose pagination parameters.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = GREENHOUSE_API_URL,
        timeout: httpx.Timeout | float = 10.0,
        company: str = "unconfigured",
    ) -> None:
        super().__init__(scraper_name="greenhouse", company=company, client=client, timeout=timeout)
        self._base_url = base_url.rstrip("/")

    async def fetch_jobs(self, company: str | None = None) -> list[RawJobPayload]:
        """Return every valid active job from a public Greenhouse board."""
        board_token = (company or self.company).strip()
        if not board_token:
            raise ValueError("company board token must be non-empty")
        if self._closed:
            raise RuntimeError("GreenhouseScraper is closed")

        url = f"{self._base_url}/{quote(board_token, safe='')}/jobs"
        try:
            response = await self._client.get(
                url,
                params={"content": "true"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GreenhouseError(f"Greenhouse request failed: {exc}") from exc
        if response.status_code == 404:
            raise GreenhouseBoardNotFoundError(f"Greenhouse board not found: {board_token}")
        if response.status_code in {429, 503}:
            raise GreenhouseRateLimitError(
                f"Greenhouse temporarily rejected the board request (HTTP {response.status_code})"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GreenhouseError(
                f"Greenhouse returned HTTP {response.status_code} for board {board_token}"
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise GreenhouseError("Greenhouse returned invalid JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("jobs"), list):
            raise GreenhouseError("Greenhouse response is missing a jobs list")

        jobs: list[RawJobPayload] = []
        for item in body["jobs"]:
            mapped = self._map_job(board_token, item)
            if mapped is not None:
                jobs.append(mapped)
        return jobs

    async def scrape(self, company: str) -> tuple[RawJobPayload, ...]:
        """Base-scraper-compatible alias for fetching a board."""
        return tuple(await self.fetch_jobs(company))

    async def aclose(self) -> None:
        """Close the HTTP client only when this scraper created it."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    @staticmethod
    def _map_job(company: str, item: object) -> RawJobPayload | None:
        if not isinstance(item, Mapping):
            return None
        job_id = item.get("id")
        title = GreenhouseScraper._text(item.get("title"))
        apply_url = GreenhouseScraper._absolute_http_url(item.get("absolute_url"))
        if job_id is None or not title or apply_url is None:
            return None

        locations = GreenhouseScraper._locations(item)
        metadata = {
            "id": job_id,
            "updated_at": item.get("updated_at"),
            "requisition_id": item.get("requisition_id"),
            "content": item.get("content"),
            "metadata": item.get("metadata"),
            "departments": item.get("departments"),
            "offices": item.get("offices"),
        }
        return RawJobPayload(
            source="greenhouse",
            source_id=f"greenhouse:{company}:{job_id}",
            company=company,
            title=title,
            apply_url=apply_url,
            location=locations,
            is_closed=False,
            payload=metadata,
        )

    @staticmethod
    def _locations(item: Mapping[object, object]) -> list[str]:
        values: list[str] = []
        location = item.get("location")
        if isinstance(location, Mapping):
            name = GreenhouseScraper._text(location.get("name"))
            if name:
                values.append(name)
        offices = item.get("offices")
        if isinstance(offices, list):
            for office in offices:
                if not isinstance(office, Mapping):
                    continue
                office_location = office.get("location")
                name = None
                if isinstance(office_location, str):
                    name = GreenhouseScraper._text(office_location)
                elif isinstance(office_location, Mapping):
                    name = GreenhouseScraper._text(office_location.get("name"))
                if name:
                    values.append(name)
        return list(dict.fromkeys(values)) or ["Unspecified"]

    @staticmethod
    def _text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = _WHITESPACE.sub(" ", value).strip()
        return normalized or None

    @staticmethod
    def _absolute_http_url(value: object) -> str | None:
        normalized = GreenhouseScraper._text(value)
        if normalized is None:
            return None
        parsed = urlparse(normalized)
        return normalized if parsed.scheme in {"http", "https"} and parsed.netloc else None
