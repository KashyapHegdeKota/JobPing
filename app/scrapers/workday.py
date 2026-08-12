"""Browser-driven scraper for JavaScript-rendered Workday job boards."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin

from app.schemas.job import RawJobPayload
from app.scrapers.base import BaseScraper, ScraperError


class Locator(Protocol):
    async def count(self) -> int: ...
    def nth(self, index: int) -> Locator: ...
    def locator(self, selector: str) -> Locator: ...
    async def inner_text(self, *, timeout: float | None = None) -> str: ...
    async def get_attribute(self, name: str, *, timeout: float | None = None) -> str | None: ...
    async def is_disabled(self, *, timeout: float | None = None) -> bool: ...
    async def click(self, *, timeout: float | None = None) -> None: ...


class Page(Protocol):
    async def goto(self, url: str, *, timeout: float | None = None) -> object: ...
    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> object: ...
    def locator(self, selector: str) -> Locator: ...
    async def close(self) -> None: ...


PageFactory = Callable[[], Awaitable[Page]]


@dataclass(frozen=True, slots=True)
class WorkdaySelectors:
    cards: str = '[data-automation-id="jobItem"]'
    title: str = '[data-automation-id="jobTitle"]'
    location: str = '[data-automation-id="locations"]'
    link: str = "a"
    closed: str | None = '[data-automation-id="jobStatus"]'
    next: str = '[data-automation-id="pagination-next"]'


class WorkdayScraperError(ScraperError):
    """A Workday browser operation failed."""


class WorkdayScraper(BaseScraper):
    def __init__(
        self,
        *,
        company: str,
        careers_url: str,
        page_factory: PageFactory,
        selectors: WorkdaySelectors | None = None,
        max_pages: int = 20,
        navigation_timeout: float = 30_000,
        selector_timeout: float = 15_000,
    ) -> None:
        super().__init__(scraper_name="workday", company=company)
        if not careers_url.strip():
            raise ValueError("careers_url must not be empty")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.careers_url = careers_url.strip()
        self._page_factory = page_factory
        self.selectors = selectors or WorkdaySelectors()
        self.max_pages = max_pages
        self.navigation_timeout = navigation_timeout
        self.selector_timeout = selector_timeout

    async def fetch_jobs(self) -> list[RawJobPayload]:
        page = await self._page_factory()
        try:
            await page.goto(self.careers_url, timeout=self.navigation_timeout)
            jobs: list[RawJobPayload] = []
            seen_pages: set[tuple[str, ...]] = set()
            for _ in range(self.max_pages):
                await page.wait_for_selector(self.selectors.cards, timeout=self.selector_timeout)
                page_jobs = await self._extract_page(page)
                signature = tuple(job.source_id or job.apply_url or "" for job in page_jobs)
                if signature in seen_pages:
                    break
                seen_pages.add(signature)
                jobs.extend(page_jobs)
                if not await self._advance(page):
                    break
            return jobs
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WorkdayScraperError(f"Workday browser scrape failed for {self.company}") from exc
        finally:
            await page.close()

    async def _extract_page(self, page: Page) -> list[RawJobPayload]:
        cards = page.locator(self.selectors.cards)
        jobs: list[RawJobPayload] = []
        for index in range(await cards.count()):
            try:
                card = cards.nth(index)
                title = (await card.locator(self.selectors.title).inner_text()).strip()
                location = (await card.locator(self.selectors.location).inner_text()).strip()
                href = await card.locator(self.selectors.link).get_attribute("href")
                if not title or not href:
                    continue
                url = urljoin(self.careers_url, href.strip())
                status = ""
                if self.selectors.closed:
                    status_locator = card.locator(self.selectors.closed)
                    if await status_locator.count():
                        status = (await status_locator.inner_text()).strip()
                jobs.append(
                    RawJobPayload(
                        source="workday",
                        source_id=url,
                        company=self.company,
                        title=title,
                        location=location or None,
                        apply_url=url,
                        is_closed=status.casefold() == "closed",
                        payload={
                            "provider": "workday",
                            "careers_url": self.careers_url,
                            "status": status,
                        },
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
        return jobs

    async def _advance(self, page: Page) -> bool:
        next_button = page.locator(self.selectors.next)
        if await next_button.count() == 0 or await next_button.is_disabled(
            timeout=self.selector_timeout
        ):
            return False
        await next_button.click(timeout=self.selector_timeout)
        return True


__all__ = ["Page", "PageFactory", "WorkdayScraper", "WorkdayScraperError", "WorkdaySelectors"]
