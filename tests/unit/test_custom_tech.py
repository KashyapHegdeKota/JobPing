"""Fixture-driven tests for custom enterprise portal scrapers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from app.scrapers.custom_tech import AmazonJobsScraper, CustomPortalError, MetaCareersScraper
from app.scrapers.proxy import ProxyEndpoint


@dataclass
class Response:
    status: int


class Page:
    def __init__(
        self,
        *,
        statuses: list[int] | None = None,
        dom: object = None,
        advances: list[bool] | None = None,
    ) -> None:
        self.statuses = statuses or [200]
        self.dom = dom
        self.advances = advances or [False]
        self.visits: list[str] = []
        self.closed = False

    async def goto(self, url: str, **kwargs: object) -> Response:
        del kwargs
        self.visits.append(url)
        return Response(self.statuses[min(len(self.visits) - 1, len(self.statuses) - 1)])

    async def evaluate(self, expression: str, arg: object | None = None) -> object:
        del arg
        if "extract" in expression:
            return self.dom
        return self.advances.pop(0) if self.advances else False

    async def close(self) -> None:
        self.closed = True


class Browser:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def new_page(self) -> Page:
        return self.page


class Interceptor:
    def __init__(self, batches: list[list[object]]) -> None:
        self.batches = batches

    async def capture(self, page: Page) -> list[object]:
        del page
        return self.batches.pop(0) if self.batches else []


class Proxy:
    def __init__(self) -> None:
        self.endpoint = ProxyEndpoint.parse("http://proxy:80")
        self.successes = 0
        self.failures: list[int | None] = []

    async def acquire(self) -> ProxyEndpoint:
        return self.endpoint

    async def report_success(self, endpoint: ProxyEndpoint) -> None:
        assert endpoint == self.endpoint
        self.successes += 1

    async def report_failure(self, endpoint: ProxyEndpoint, status: int | None = None) -> None:
        assert endpoint == self.endpoint
        self.failures.append(status)


@pytest.mark.asyncio
async def test_amazon_json_maps_relative_url_dedups_and_skips_malformed() -> None:
    page = Page(advances=[False])
    records = {
        "jobs": [
            {
                "job_id": "1",
                "job_title": "SDE Intern",
                "job_path": "/en/jobs/1",
                "location_name": "Seattle",
            },
            {"job_id": "1", "job_title": "Duplicate", "job_path": "/en/jobs/other"},
            {"job_id": "2"},
        ]
    }
    scraper = AmazonJobsScraper(browser=Browser(page), interceptor=Interceptor([[records]]))
    jobs = await scraper.fetch_jobs()
    assert len(jobs) == 1
    assert jobs[0].apply_url == "https://www.amazon.jobs/en/jobs/1"
    assert jobs[0].location == ["Seattle"]
    assert page.closed
    await scraper.aclose()


@pytest.mark.asyncio
async def test_meta_graphql_nested_data_and_bounded_pagination() -> None:
    page = Page(advances=[True, True, True])
    payload = {
        "data": {
            "searchResults": [
                {
                    "id": "m1",
                    "name": "University Grad",
                    "job_url": "/jobs/1",
                    "locations": [{"name": "Menlo Park"}],
                }
            ]
        }
    }
    scraper = MetaCareersScraper(
        browser=Browser(page), interceptor=Interceptor([[payload], [payload]]), max_pages=2
    )
    jobs = await scraper.fetch_jobs()
    assert len(jobs) == 1
    assert len(page.visits) == 2
    assert page.visits[1].endswith("?page=2")
    await scraper.aclose()


@pytest.mark.asyncio
async def test_dom_fallback_and_scroll_termination() -> None:
    page = Page(
        dom=[{"id": "1", "title": "Intern", "url": "/jobs/1", "location": "Remote"}],
        advances=[False],
    )
    scraper = MetaCareersScraper(browser=Browser(page))
    assert len(await scraper.fetch_jobs()) == 1
    assert len(page.visits) == 1
    await scraper.aclose()


@pytest.mark.asyncio
async def test_proxy_success_failure_and_cleanup() -> None:
    good_page = Page(dom=[], advances=[False])
    good_proxy = Proxy()
    good = AmazonJobsScraper(browser=Browser(good_page), proxy_manager=good_proxy)  # type: ignore[arg-type]
    await good.fetch_jobs()
    assert good_proxy.successes == 1
    await good.aclose()

    bad_page = Page(statuses=[429])
    bad_proxy = Proxy()
    bad = MetaCareersScraper(browser=Browser(bad_page), proxy_manager=bad_proxy)  # type: ignore[arg-type]
    with pytest.raises(CustomPortalError, match="429"):
        await bad.fetch_jobs()
    assert bad_proxy.failures == [429]
    assert bad_page.closed
    await bad.aclose()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_page() -> None:
    class CancelPage(Page):
        async def goto(self, url: str, **kwargs: object) -> Response:
            del url, kwargs
            raise asyncio.CancelledError

    page = CancelPage()
    scraper = AmazonJobsScraper(browser=Browser(page))
    with pytest.raises(asyncio.CancelledError):
        await scraper.fetch_jobs()
    assert page.closed
    await scraper.aclose()
