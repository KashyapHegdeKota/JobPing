"""Browser-backed scrapers for dynamic Amazon and Meta career portals."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Protocol
from urllib.parse import urljoin, urlparse

import httpx

from app.schemas.job import RawJobPayload
from app.scrapers.base import BaseScraper, ScraperError
from app.scrapers.proxy import ProxyEndpoint, ProxyManager
from app.utils.resilience import external_retry

_LOGGER = logging.getLogger(__name__)


class ResponseLike(Protocol):
    @property
    def status(self) -> int: ...


class PageLike(Protocol):
    async def goto(self, url: str, **kwargs: object) -> ResponseLike | None: ...
    async def evaluate(self, expression: str, arg: object | None = None) -> object: ...
    async def close(self) -> None: ...


class BrowserLike(Protocol):
    async def new_page(self) -> PageLike: ...


class PayloadInterceptor(Protocol):
    """Narrow compatibility boundary for the parallel interception module."""

    async def capture(self, page: PageLike) -> Sequence[object]: ...


class CustomPortalError(ScraperError):
    """A dynamic career portal could not be scraped safely."""


class CustomTechScraper(BaseScraper):
    """Shared bounded browser workflow; browser hardening is not a bypass guarantee."""

    source: str
    start_url: str

    def __init__(
        self,
        *,
        scraper_name: str,
        company: str,
        browser: BrowserLike,
        interceptor: PayloadInterceptor | None = None,
        proxy_manager: ProxyManager | None = None,
        max_pages: int = 5,
    ) -> None:
        super().__init__(scraper_name=scraper_name, company=company, client=httpx.AsyncClient())
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._browser = browser
        self._interceptor = interceptor
        self._proxy_manager = proxy_manager
        self._max_pages = max_pages

    async def fetch_jobs(self) -> list[RawJobPayload]:
        page = await self._browser.new_page()
        proxy: ProxyEndpoint | None = None
        if self._proxy_manager is not None:
            proxy = await self._proxy_manager.acquire()
        try:
            records: list[object] = []
            if self._interceptor is not None and hasattr(self._interceptor, "attach"):
                await self._interceptor.capture(page)
            for index in range(self._max_pages):
                response = await self._navigate(page, self._page_url(index))
                status = response.status if response is not None else 200
                if status in {403, 429, 503}:
                    if proxy is not None and self._proxy_manager is not None:
                        await self._proxy_manager.report_failure(proxy, status)
                    raise CustomPortalError(f"{self.source} portal returned HTTP {status}")
                if status >= 400:
                    raise CustomPortalError(f"{self.source} portal returned HTTP {status}")
                captured = (
                    list(await self._interceptor.capture(page))
                    if self._interceptor is not None
                    else []
                )
                records.extend(self._flatten(captured))
                if not captured:
                    fallback = await page.evaluate("jobping:extract-dom-cards")
                    records.extend(self._flatten([fallback]))
                more = await page.evaluate("jobping:advance-or-scroll", index)
                if not bool(more):
                    break
            jobs = self._map_unique(records)
            if proxy is not None and self._proxy_manager is not None:
                await self._proxy_manager.report_success(proxy)
            return jobs
        except asyncio.CancelledError:
            raise
        finally:
            await page.close()
            if self._owns_client:
                await self.aclose()

    @external_retry
    async def _navigate(self, page: PageLike, url: str) -> ResponseLike | None:
        return await page.goto(url, wait_until="domcontentloaded")

    def _map_unique(self, records: Sequence[object]) -> list[RawJobPayload]:
        jobs: list[RawJobPayload] = []
        seen: set[str] = set()
        for record in records:
            try:
                mapped = self.map_record(record)
            except Exception:
                _LOGGER.exception("custom_portal.job.mapping_failed", extra={"source": self.source})
                continue
            if mapped is None:
                continue
            identity = mapped.source_id or mapped.apply_url or ""
            if identity and identity not in seen:
                seen.add(identity)
                jobs.append(mapped)
        return jobs

    def map_record(self, record: object) -> RawJobPayload | None:
        raise NotImplementedError

    def _page_url(self, index: int) -> str:
        separator = "&" if "?" in self.start_url else "?"
        return self.start_url if index == 0 else f"{self.start_url}{separator}page={index + 1}"

    @staticmethod
    def _flatten(values: Sequence[object]) -> list[object]:
        result: list[object] = []
        stack = list(values)
        while stack:
            value = stack.pop(0)
            if isinstance(value, list):
                stack[0:0] = value
            elif isinstance(value, Mapping):
                nested = next(
                    (
                        value[key]
                        for key in ("jobs", "results", "searchResults", "data")
                        if key in value
                    ),
                    None,
                )
                if isinstance(nested, (list, Mapping)):
                    stack.insert(0, nested)
                else:
                    result.append(value)
        return result


class AmazonJobsScraper(CustomTechScraper):
    source = "amazon_jobs"
    start_url = "https://www.amazon.jobs/en/teams/internships-for-students"

    def __init__(self, *, browser: BrowserLike, **kwargs: object) -> None:
        super().__init__(scraper_name=self.source, company="Amazon", browser=browser, **kwargs)

    def map_record(self, record: object) -> RawJobPayload | None:
        return _map_common(
            record,
            source=self.source,
            company="Amazon",
            base="https://www.amazon.jobs",
            id_keys=("id", "job_id", "jobId"),
            title_keys=("title", "job_title"),
            url_keys=("job_path", "url", "apply_url"),
            location_keys=("location", "location_name", "locations"),
        )


class MetaCareersScraper(CustomTechScraper):
    source = "meta_careers"
    start_url = "https://www.metacareers.com/jobs"

    def __init__(self, *, browser: BrowserLike, **kwargs: object) -> None:
        super().__init__(scraper_name=self.source, company="Meta", browser=browser, **kwargs)

    def map_record(self, record: object) -> RawJobPayload | None:
        return _map_common(
            record,
            source=self.source,
            company="Meta",
            base="https://www.metacareers.com",
            id_keys=("id", "job_id"),
            title_keys=("title", "name"),
            url_keys=("url", "apply_url", "job_url"),
            location_keys=("locations", "location"),
        )


def _map_common(
    record: object,
    *,
    source: str,
    company: str,
    base: str,
    id_keys: tuple[str, ...],
    title_keys: tuple[str, ...],
    url_keys: tuple[str, ...],
    location_keys: tuple[str, ...],
) -> RawJobPayload | None:
    if not isinstance(record, Mapping):
        return None
    identifier = _first(record, id_keys)
    title = _first(record, title_keys)
    url = _first(record, url_keys)
    if not title or not url:
        return None
    absolute = urljoin(base, url)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    locations: list[str] = []
    for key in location_keys:
        value = record.get(key)
        if isinstance(value, str) and (cleaned := " ".join(value.split())):
            locations.append(cleaned)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            locations.extend(text for item in value if (text := _location(item)))
    return RawJobPayload(
        source=source,
        source_id=f"{source}:{identifier}" if identifier else absolute,
        company=company,
        title=title,
        apply_url=absolute,
        location=list(dict.fromkeys(locations)) or ["Unspecified"],
        is_closed=False,
        payload={"provider": source, "raw": dict(record)},
    )


def _first(record: Mapping[object, object], keys: tuple[str, ...]) -> str | None:
    return next((text for key in keys if (text := _text(record.get(key)))), None)


def _text(value: object) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _location(value: object) -> str | None:
    if isinstance(value, Mapping):
        return _first(value, ("name", "city", "display_name"))
    return _text(value)


__all__ = [
    "AmazonJobsScraper",
    "CustomPortalError",
    "CustomTechScraper",
    "MetaCareersScraper",
    "PayloadInterceptor",
]
