"""Tests for the browser-independent Workday scraper orchestration."""

from __future__ import annotations

import pytest
from app.scrapers.workday import WorkdayScraper, WorkdayScraperError


class FakeLocator:
    def __init__(self, values: list[dict[str, object]] | None = None, value: object = None) -> None:
        self.values = values or []
        self.value = value

    async def count(self) -> int:
        return len(self.values) if self.values else int(self.value is not None)

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(value=self.values[index])

    def locator(self, selector: str) -> FakeLocator:
        if isinstance(self.value, dict):
            return FakeLocator(value=self.value.get(selector))
        return self

    async def inner_text(self, *, timeout: float | None = None) -> str:
        if isinstance(self.value, Exception):
            raise self.value
        return str(self.value or "")

    async def get_attribute(self, name: str, *, timeout: float | None = None) -> str | None:
        return self.value if isinstance(self.value, str) else None

    async def is_disabled(self, *, timeout: float | None = None) -> bool:
        return bool(self.value)

    async def click(self, *, timeout: float | None = None) -> None:
        return None


class FakePage:
    def __init__(self, pages: list[list[dict[str, object]]], *, fail: bool = False) -> None:
        self.pages, self.index, self.closed, self.fail = pages, 0, False, fail

    async def goto(self, url: str, *, timeout: float | None = None) -> None:
        if self.fail:
            raise TimeoutError

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:
        return None

    def locator(self, selector: str) -> FakeLocator:
        if selector.startswith('[data-automation-id="jobItem'):
            return FakeLocator(values=self.pages[self.index])
        page = self

        class Next(FakeLocator):
            async def count(self) -> int:
                return int(page.index < len(page.pages) - 1)

            async def is_disabled(self, *, timeout: float | None = None) -> bool:
                return False

            async def click(self, *, timeout: float | None = None) -> None:
                page.index += 1

        return Next()

    async def close(self) -> None:
        self.closed = True


def card(title: str = "Intern", href: str = "/job/1", status: str = "") -> dict[str, str]:
    return {
        '[data-automation-id="jobTitle"]': title,
        '[data-automation-id="locations"]': "New York",
        "a": href,
        '[data-automation-id="jobStatus"]': status,
    }


@pytest.mark.asyncio
async def test_maps_multiple_pages_and_run_metrics() -> None:
    page = FakePage([[card()], [card("New Grad", "/job/2", "Closed")]])
    scraper = WorkdayScraper(
        company="Acme", careers_url="https://acme.test/jobs", page_factory=lambda: _page(page)
    )
    jobs = await scraper.run()
    assert [job.title for job in jobs] == ["Intern", "New Grad"]
    assert jobs[0].apply_url == "https://acme.test/job/1"
    assert jobs[1].is_closed is True
    assert scraper.last_run is not None and scraper.last_run.jobs_count == 2
    assert page.closed is True


@pytest.mark.asyncio
async def test_absent_next_stops_and_malformed_card_is_skipped() -> None:
    page = FakePage([[card(), card(title="", href="")]])
    scraper = WorkdayScraper(
        company="Acme", careers_url="https://acme.test/jobs", page_factory=lambda: _page(page)
    )
    assert len(await scraper.fetch_jobs()) == 1
    assert page.closed


@pytest.mark.asyncio
async def test_repeated_page_signature_stops_loop() -> None:
    page = FakePage([[card()], [card()]])
    scraper = WorkdayScraper(
        company="Acme", careers_url="https://acme.test", page_factory=lambda: _page(page)
    )
    assert len(await scraper.fetch_jobs()) == 1


@pytest.mark.asyncio
async def test_timeout_is_mapped_and_page_is_closed() -> None:
    page = FakePage([], fail=True)
    scraper = WorkdayScraper(
        company="Acme", careers_url="https://acme.test", page_factory=lambda: _page(page)
    )
    with pytest.raises(WorkdayScraperError):
        await scraper.run()
    assert page.closed and scraper.failure_count == 1


async def _page(page: FakePage) -> FakePage:
    return page
