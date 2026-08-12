"""Tests for the standard asynchronous scraper boundary."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from app.schemas.job import RawJobPayload
from app.scrapers.base import BaseScraper, ScraperError, ScraperRequestError


def payload() -> RawJobPayload:
    return RawJobPayload(source="test", company="Acme", title="Engineer")


class StubScraper(BaseScraper):
    def __init__(
        self,
        *,
        jobs: list[RawJobPayload] | None = None,
        failure: Exception | None = None,
        client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(
            scraper_name="stub",
            company="Acme",
            client=client,
            logger=logger,
        )
        self.jobs = jobs or []
        self.failure = failure

    async def fetch_jobs(self) -> list[RawJobPayload]:
        if self.failure is not None:
            raise self.failure
        return self.jobs

    async def request(self, url: str) -> httpx.Response:
        return await self._request("GET", url)


class BlockingScraper(BaseScraper):
    def __init__(self) -> None:
        super().__init__(scraper_name="blocking", company="Acme")
        self.started = asyncio.Event()

    async def fetch_jobs(self) -> list[RawJobPayload]:
        self.started.set()
        await asyncio.Event().wait()
        return []


def test_base_scraper_is_abstract() -> None:
    with pytest.raises(TypeError, match="abstract"):
        BaseScraper(scraper_name="base", company="Acme")  # type: ignore[abstract]


async def test_run_records_success_metrics_and_structured_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scraper = StubScraper(jobs=[payload()])
    with caplog.at_level(logging.INFO):
        result = await scraper.run()

    assert result == [payload()]
    assert scraper.success_count == 1
    assert scraper.failure_count == 0
    assert scraper.last_run is not None
    assert scraper.last_run.succeeded is True
    assert scraper.last_run.jobs_count == 1
    assert scraper.last_run.elapsed_seconds >= 0
    record = next(item for item in caplog.records if item.message == "scraper.fetch.succeeded")
    assert record.scraper_name == "stub"  # type: ignore[attr-defined]
    assert record.company == "Acme"  # type: ignore[attr-defined]
    assert record.jobs_count == 1  # type: ignore[attr-defined]
    await scraper.aclose()


async def test_run_records_failure_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    scraper = StubScraper(failure=ValueError("bad payload"))
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="bad payload"):
        await scraper.run()

    assert scraper.success_count == 0
    assert scraper.failure_count == 1
    assert scraper.last_run is not None
    assert scraper.last_run.succeeded is False
    assert "scraper.fetch.failed" in caplog.messages
    await scraper.aclose()


async def test_cancellation_propagates_without_becoming_failure() -> None:
    scraper = BlockingScraper()
    task = asyncio.create_task(scraper.run())
    await scraper.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert scraper.failure_count == 0
    assert scraper.last_run is None
    await scraper.aclose()


async def test_injected_client_is_not_closed_by_context_manager() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    async with StubScraper(client=client) as scraper:
        assert (await scraper.request("https://jobs.example")).status_code == 200

    assert client.is_closed is False
    with pytest.raises(ScraperError, match="closed"):
        await scraper.run()
    await client.aclose()


async def test_owned_client_is_closed_and_close_is_idempotent() -> None:
    scraper = StubScraper()
    client = scraper._client
    await scraper.aclose()
    await scraper.aclose()
    assert client.is_closed is True


async def test_request_errors_are_wrapped_with_source_context() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        scraper = StubScraper(client=client)
        with pytest.raises(ScraperRequestError, match="stub request failed") as captured:
            await scraper.request("https://jobs.example")
        assert isinstance(captured.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("field", ["scraper_name", "company"])
def test_identity_must_not_be_blank(field: str) -> None:
    values = {"scraper_name": "stub", "company": "Acme", field: "  "}

    class IdentityScraper(BaseScraper):
        async def fetch_jobs(self) -> list[RawJobPayload]:
            return []

    with pytest.raises(ValueError, match=field):
        IdentityScraper(**values)
