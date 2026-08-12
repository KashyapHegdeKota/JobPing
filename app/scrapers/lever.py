"""Asynchronous client for Lever's public postings API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import quote

import httpx

from app.schemas.job import RawJobPayload
from app.scrapers.base import BaseScraper

_API_ROOT = "https://api.lever.co/v0/postings"
_DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class LeverError(RuntimeError):
    """Base error raised for Lever API failures."""


class LeverResponseError(LeverError):
    """Raised when Lever returns an unusable response."""


class LeverScraper(BaseScraper):
    """Fetch and map public postings for one Lever site token."""

    source = "lever"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        timeout: httpx.Timeout | float = _DEFAULT_TIMEOUT,
        company: str = "unconfigured",
    ) -> None:
        super().__init__(scraper_name="lever", company=company, client=client, timeout=timeout)

    async def fetch_jobs(self) -> list[RawJobPayload]:
        """Fetch jobs for the configured Lever company token."""
        return list(await self.scrape(self.company))

    async def scrape(self, company: str) -> tuple[RawJobPayload, ...]:
        """Return valid postings for a Lever company/site token."""
        site = _site_token(company)
        try:
            response = await self._client.get(
                f"{_API_ROOT}/{quote(site, safe='')}",
                params={"mode": "json"},
                timeout=self._timeout,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise LeverError(f"Lever request timed out for {site!r}") from exc
        except httpx.HTTPStatusError as exc:
            raise LeverError(
                f"Lever returned HTTP {exc.response.status_code} for {site!r}"
            ) from exc
        except httpx.RequestError as exc:
            raise LeverError(f"Lever request failed for {site!r}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise LeverResponseError("Lever response was not valid JSON") from exc
        if not isinstance(data, list):
            raise LeverResponseError("Lever response must be a JSON array")

        postings: list[RawJobPayload] = []
        for entry in data:
            mapped = _map_posting(entry, company=site)
            if mapped is not None:
                postings.append(mapped)
        return tuple(postings)

    async def fetch(self, company: str) -> tuple[RawJobPayload, ...]:
        """Compatibility alias for BaseScraper implementations using ``fetch``."""
        return await self.scrape(company)


def _site_token(value: str) -> str:
    token = value.strip()
    if not token or len(token) > 200 or any(character in token for character in "/\\?#"):
        raise ValueError("Lever company must be a non-empty site token")
    return token


def _map_posting(entry: object, *, company: str) -> RawJobPayload | None:
    if not isinstance(entry, Mapping):
        return None
    identifier = _text(entry.get("id"))
    title = _text(entry.get("text"))
    apply_url = _first_text(entry, "applyUrl", "hostedUrl")
    if not identifier or not title or not _is_http_url(apply_url):
        return None

    categories = entry.get("categories")
    category_map = categories if isinstance(categories, Mapping) else {}
    locations = _locations(category_map, entry)
    payload = dict(entry)
    return RawJobPayload(
        source="lever",
        source_id=identifier,
        company=company,
        title=title,
        apply_url=apply_url,
        location=locations or None,
        is_closed=False,
        payload={"provider": "lever", "site": company, "raw": payload},
    )


def _locations(categories: Mapping[object, object], entry: Mapping[object, object]) -> list[str]:
    values: list[str] = []
    _append_locations(values, categories.get("location"))
    _append_locations(values, categories.get("allLocations"))
    _append_locations(values, entry.get("workplaceType"))
    return list(dict.fromkeys(values))


def _append_locations(target: list[str], value: object) -> None:
    if isinstance(value, str):
        for part in value.replace("\n", ";").split(";"):
            if cleaned := part.strip():
                target.append(cleaned)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            if cleaned := _text(item):
                target.append(cleaned)


def _first_text(mapping: Mapping[object, object], *keys: str) -> str | None:
    return next((text for key in keys if (text := _text(mapping.get(key)))), None)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _is_http_url(value: str | None) -> bool:
    return value is not None and value.lower().startswith(("https://", "http://"))
