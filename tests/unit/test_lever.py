"""Implementation tests for the Lever public API scraper."""

import httpx
import pytest
from app.scrapers.lever import LeverError, LeverResponseError, LeverScraper


@pytest.mark.asyncio
async def test_fetches_and_maps_postings_with_locations() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v0/postings/acme"
        assert request.url.params.get("mode") == "json"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "job-1",
                    "text": " Software Engineer Intern ",
                    "hostedUrl": "https://jobs.lever.co/acme/job-1",
                    "applyUrl": "https://jobs.lever.co/acme/job-1/apply",
                    "categories": {
                        "location": "New York; Remote",
                        "allLocations": ["Remote", "Boston"],
                    },
                    "workplaceType": "hybrid",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await LeverScraper(client).scrape(" acme ")

    assert len(results) == 1
    job = results[0]
    assert job.source == "lever"
    assert job.source_id == "job-1"
    assert job.company == "acme"
    assert job.title == "Software Engineer Intern"
    assert job.apply_url == "https://jobs.lever.co/acme/job-1/apply"
    assert job.location == ["New York", "Remote", "Boston", "hybrid"]
    assert job.is_closed is False
    assert job.payload["raw"]["id"] == "job-1"


@pytest.mark.asyncio
async def test_skips_malformed_entries_deterministically() -> None:
    payload = [None, "bad", {}, {"id": "1", "text": "Missing URL"}, {"id": "2", "text": 4}]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await LeverScraper(client).fetch("acme") == ()


@pytest.mark.asyncio
async def test_falls_back_to_hosted_url_and_tolerates_null_optionals() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "job-2",
                    "text": "New Grad Engineer",
                    "applyUrl": None,
                    "hostedUrl": "https://jobs.lever.co/acme/job-2",
                    "categories": None,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = (await LeverScraper(client).scrape("acme"))[0]

    assert result.apply_url == "https://jobs.lever.co/acme/job-2"
    assert result.location is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not-json", b'{"postings": []}'])
async def test_rejects_invalid_response_shapes(body: bytes) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LeverResponseError):
            await LeverScraper(client).scrape("acme")


@pytest.mark.asyncio
async def test_maps_http_failures_without_leaking_httpx_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LeverError, match="HTTP 429"):
            await LeverScraper(client).scrape("acme")


@pytest.mark.parametrize("company", ["", "../acme", "acme?mode=xml", "acme/jobs"])
@pytest.mark.asyncio
async def test_rejects_unsafe_site_tokens(company: str) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError):
            await LeverScraper(client).scrape(company)
