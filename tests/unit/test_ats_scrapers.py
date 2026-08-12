"""Shared response-mapping and error contracts for direct ATS scrapers."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from app.schemas.job import RawJobPayload
from app.scrapers.base import BaseScraper
from app.scrapers.greenhouse import (
    GreenhouseBoardNotFoundError,
    GreenhouseError,
    GreenhouseRateLimitError,
    GreenhouseScraper,
)
from app.scrapers.lever import LeverError, LeverResponseError, LeverScraper

type ScraperFactory = Callable[[httpx.AsyncClient], BaseScraper]


def greenhouse(client: httpx.AsyncClient) -> GreenhouseScraper:
    return GreenhouseScraper(client=client, company="acme engineering")


def lever(client: httpx.AsyncClient) -> LeverScraper:
    return LeverScraper(client, company="acme engineering")


@pytest.mark.parametrize("factory", [greenhouse, lever])
async def test_scrapers_conform_to_base_contract_and_run_metrics(
    factory: ScraperFactory,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "greenhouse" in request.url.host:
            body: object = {
                "jobs": [
                    {
                        "id": 42,
                        "title": "Engineer Intern",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                        "location": {"name": "Remote"},
                    }
                ]
            }
        else:
            body = [
                {
                    "id": "job-42",
                    "text": "Engineer Intern",
                    "applyUrl": "https://jobs.lever.co/acme/job-42/apply",
                    "categories": {"location": "Remote"},
                }
            ]
        return httpx.Response(200, json=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scraper = factory(client)
        assert isinstance(scraper, BaseScraper)
        jobs = await scraper.run()

    assert isinstance(jobs, list)
    assert all(isinstance(job, RawJobPayload) for job in jobs)
    assert scraper.success_count == 1
    assert scraper.failure_count == 0
    assert scraper.last_run is not None
    assert scraper.last_run.jobs_count == 1
    assert scraper.last_run.succeeded is True


async def test_greenhouse_maps_realistic_response_and_encodes_board_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.split(b"?", 1)[0] == (
            b"/v1/boards/acme%20engineering%2Fplatform/jobs"
        )
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
                        "updated_at": "2026-08-11T12:00:00-04:00",
                        "requisition_id": "REQ-42",
                        "content": "<p>Build systems</p>",
                        "metadata": [{"name": "Team", "value": "Platform"}],
                        "departments": [{"name": "Engineering"}],
                    },
                    {"id": 43, "title": None, "absolute_url": None},
                    "malformed",
                ]
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await GreenhouseScraper(client=client).fetch_jobs(" acme engineering/platform ")

    assert len(jobs) == 1
    assert jobs[0].model_dump() == {
        "source": "greenhouse",
        "source_id": "greenhouse:acme engineering/platform:42",
        "company": "acme engineering/platform",
        "title": "Software Engineer Intern",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/42",
        "location": ["New York, NY", "Remote"],
        "season": None,
        "job_type": None,
        "is_closed": False,
        "observed_at": None,
        "payload": {
            "id": 42,
            "updated_at": "2026-08-11T12:00:00-04:00",
            "requisition_id": "REQ-42",
            "content": "<p>Build systems</p>",
            "metadata": [{"name": "Team", "value": "Platform"}],
            "departments": [{"name": "Engineering"}],
            "offices": [
                {"location": "Remote"},
                {"location": {"name": "New York, NY"}},
            ],
        },
    }


async def test_lever_maps_realistic_response_and_multi_locations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.split(b"?", 1)[0] == (b"/v0/postings/acme%20engineering")
        assert dict(request.url.params) == {"mode": "json"}
        assert request.headers["accept"] == "application/json"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "job-1",
                    "text": " Software   Engineer Intern ",
                    "hostedUrl": "https://jobs.lever.co/acme/job-1",
                    "applyUrl": "https://jobs.lever.co/acme/job-1/apply",
                    "categories": {
                        "location": "New York; Remote",
                        "allLocations": ["Remote", "Boston"],
                    },
                    "workplaceType": "hybrid",
                    "createdAt": 1_786_464_000_000,
                },
                {"id": "missing-url", "text": "Incomplete"},
                None,
            ],
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await LeverScraper(client, company=" acme engineering ").fetch_jobs()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "lever"
    assert job.source_id == "job-1"
    assert job.company == "acme engineering"
    assert job.title == "Software Engineer Intern"
    assert job.apply_url == "https://jobs.lever.co/acme/job-1/apply"
    assert job.location == ["New York", "Remote", "Boston", "hybrid"]
    assert job.is_closed is False
    assert job.payload == {
        "provider": "lever",
        "site": "acme engineering",
        "raw": {
            "id": "job-1",
            "text": " Software   Engineer Intern ",
            "hostedUrl": "https://jobs.lever.co/acme/job-1",
            "applyUrl": "https://jobs.lever.co/acme/job-1/apply",
            "categories": {
                "location": "New York; Remote",
                "allLocations": ["Remote", "Boston"],
            },
            "workplaceType": "hybrid",
            "createdAt": 1_786_464_000_000,
        },
    }


@pytest.mark.parametrize("factory", [greenhouse, lever])
async def test_malformed_entries_are_skipped(factory: ScraperFactory) -> None:
    greenhouse_body = {"jobs": [None, "bad", {}, {"id": 1, "title": "No URL"}]}
    lever_body = [None, "bad", {}, {"id": "1", "text": "No URL"}]

    def handler(request: httpx.Request) -> httpx.Response:
        body = greenhouse_body if "greenhouse" in request.url.host else lever_body
        return httpx.Response(200, json=body, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await factory(client).fetch_jobs() == []


@pytest.mark.parametrize(
    ("factory", "body", "error"),
    [
        (greenhouse, b"not-json", GreenhouseError),
        (greenhouse, b"[]", GreenhouseError),
        (greenhouse, b'{"jobs": null}', GreenhouseError),
        (lever, b"not-json", LeverResponseError),
        (lever, b'{"postings": []}', LeverResponseError),
    ],
)
async def test_invalid_json_or_envelope_is_rejected(
    factory: ScraperFactory, body: bytes, error: type[Exception]
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(error):
            await factory(client).fetch_jobs()


@pytest.mark.parametrize(
    ("factory", "status", "error"),
    [
        (greenhouse, 404, GreenhouseBoardNotFoundError),
        (greenhouse, 429, GreenhouseRateLimitError),
        (greenhouse, 503, GreenhouseRateLimitError),
        (greenhouse, 500, GreenhouseError),
        (lever, 404, LeverError),
        (lever, 429, LeverError),
        (lever, 500, LeverError),
    ],
)
async def test_http_errors_preserve_provider_contract(
    factory: ScraperFactory, status: int, error: type[Exception]
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(error):
            await factory(client).fetch_jobs()


@pytest.mark.parametrize("factory", [greenhouse, lever])
@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("offline")],
)
async def test_network_and_timeout_errors_are_wrapped(
    factory: ScraperFactory, failure: httpx.RequestError
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        failure.request = request
        raise failure

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((GreenhouseError, LeverError)) as captured:
            await factory(client).fetch_jobs()
        assert captured.value.__cause__ is failure


@pytest.mark.parametrize("factory", [greenhouse, lever])
async def test_injected_client_remains_caller_owned(factory: ScraperFactory) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"jobs": []} if "greenhouse" in request.url.host else [],
                request=request,
            )
        )
    )
    async with factory(client) as scraper:
        assert await scraper.fetch_jobs() == []
    assert client.is_closed is False
    await client.aclose()


@pytest.mark.parametrize(
    "scraper",
    [GreenhouseScraper(company="acme"), LeverScraper(company="acme")],
)
async def test_owned_client_is_closed_idempotently(scraper: BaseScraper) -> None:
    client = scraper._client
    await scraper.aclose()
    await scraper.aclose()
    assert client.is_closed is True


@pytest.mark.parametrize("company", ["", "../acme", "acme?mode=xml", "acme/jobs"])
async def test_lever_rejects_unsafe_site_tokens(company: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        with pytest.raises(ValueError):
            await LeverScraper(client, company=company).fetch_jobs()
