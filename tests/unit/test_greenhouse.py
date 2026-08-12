"""Implementation tests for the Greenhouse direct API client."""

from __future__ import annotations

import httpx
import pytest
from app.scrapers.greenhouse import (
    GreenhouseBoardNotFoundError,
    GreenhouseError,
    GreenhouseScraper,
)


@pytest.mark.asyncio
async def test_fetch_jobs_requests_complete_board_and_maps_valid_jobs() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.split(b"?", 1)[0] == b"/v1/boards/acme%2Fengineering/jobs"
        assert dict(request.url.params) == {"content": "true"}
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 42,
                        "title": "  Software   Engineer Intern ",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                        "location": {"name": "New York, NY"},
                        "offices": [
                            {"location": "Remote"},
                            {"location": {"name": "New York, NY"}},
                        ],
                        "content": None,
                        "updated_at": "2026-08-11T12:00:00-04:00",
                    },
                    {"id": 43, "title": None, "absolute_url": None},
                    "malformed",
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    scraper = GreenhouseScraper(client=client)
    jobs = await scraper.fetch_jobs(" acme/engineering ")

    assert len(jobs) == 1
    assert jobs[0].source == "greenhouse"
    assert jobs[0].source_id == "greenhouse:acme/engineering:42"
    assert jobs[0].company == "acme/engineering"
    assert jobs[0].title == "Software Engineer Intern"
    assert jobs[0].location == ["New York, NY", "Remote"]
    assert jobs[0].is_closed is False
    assert jobs[0].payload["content"] is None
    await scraper.aclose()
    assert not client.is_closed
    await client.aclose()


@pytest.mark.asyncio
async def test_scrape_alias_and_owned_client_lifecycle() -> None:
    scraper = GreenhouseScraper(
        client=None,
        base_url="https://example.test/v1/boards",
    )
    owned_client = scraper._client
    await scraper.aclose()
    await scraper.aclose()

    assert owned_client.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        await scraper.scrape("acme")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_fetch_jobs_maps_http_errors(status: int) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        scraper = GreenhouseScraper(client=client)
        expected = GreenhouseBoardNotFoundError if status == 404 else GreenhouseError
        with pytest.raises(expected):
            await scraper.fetch_jobs("missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"jobs": None}, ["not-an-object"]])
async def test_fetch_jobs_rejects_malformed_response(body: object) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        scraper = GreenhouseScraper(client=client)
        with pytest.raises(GreenhouseError, match="jobs list"):
            await scraper.fetch_jobs("acme")


@pytest.mark.asyncio
async def test_fetch_jobs_requires_nonempty_board_token() -> None:
    async with httpx.AsyncClient() as client:
        scraper = GreenhouseScraper(client=client)
        with pytest.raises(ValueError, match="non-empty"):
            await scraper.fetch_jobs("   ")
