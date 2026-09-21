"""Unit tests for ApplyGuy JSON ingestion."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from app.pipelines.applyguy_pipeline import ApplyGuyPipeline
from app.scrapers.applyguy import ApplyGuyFeed, ApplyGuyScraper
from app.services.deduplicator import DeduplicationState
from app.services.hasher import generate_base_hash


def feed_body() -> dict[str, object]:
    return {
        "updatedAt": "2026-09-21T20:15:30.615Z",
        "jobs": [
            {
                "id": "mercury-1",
                "company": "Mercury",
                "title": "Software Engineering Intern - Spring 2027",
                "category": "Software Engineering",
                "location": "Remote, U.S.",
                "season": "Spring 2027",
                "posted": "2026-09-21",
                "age": "Today",
                "url": "https://applyguy.ai/jobs?id=mercury-1&utm_source=feed",
                "listingUrl": "https://job-boards.greenhouse.io/mercury/jobs/6199367004",
            }
        ],
    }


def client_for(payload: object) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_internship_mapping_prefers_listing_url_and_preserves_metadata() -> None:
    client = client_for(feed_body())
    scraper = ApplyGuyScraper(ApplyGuyFeed.INTERNSHIPS, client=client)
    try:
        jobs = await scraper.fetch_jobs()
    finally:
        await scraper.aclose()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "applyguy_internships"
    assert job.source_id == "mercury-1"
    assert job.apply_url == "https://job-boards.greenhouse.io/mercury/jobs/6199367004"
    assert job.job_type == "internship"
    assert job.season == 2027
    assert job.observed_at == datetime(2026, 9, 21, 20, 15, 30, 615000, tzinfo=UTC)
    assert job.payload["category"] == "Software Engineering"
    assert job.payload["listing_url"].startswith("https://job-boards")


@pytest.mark.asyncio
async def test_new_grad_mapping_and_missing_listing_url_fallback() -> None:
    body = {
        "updatedAt": "2026-09-21T20:15:55.668Z",
        "jobs": [
            {
                "id": "adyen-1",
                "company": "Adyen",
                "title": "Software Engineer I",
                "location": "Chicago, IL",
                "eligibility": "Entry Level",
                "matchKind": "entry_level",
                "posted": "2026-09-20",
                "url": "https://applyguy.ai/jobs/adyen-1",
            }
        ],
    }
    client = client_for(body)
    scraper = ApplyGuyScraper("new-grad", client=client)
    try:
        jobs = await scraper.fetch_jobs()
    finally:
        await scraper.aclose()

    assert jobs[0].source == "applyguy_new_grad"
    assert jobs[0].job_type == "new_grad"
    assert jobs[0].apply_url == "https://applyguy.ai/jobs/adyen-1"
    assert jobs[0].payload["eligibility"] == "Entry Level"
    assert jobs[0].payload["matchKind"] == "entry_level"


@pytest.mark.asyncio
async def test_malformed_rows_are_logged_and_valid_rows_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = feed_body()
    body["jobs"] = [
        body["jobs"][0],
        {"id": "bad", "company": "Missing URL", "title": "Engineer"},
        {"id": "bad-2", "company": "Bad", "title": "Engineer", "url": "javascript:bad"},
    ]
    client = client_for(body)
    scraper = ApplyGuyScraper("internship", client=client)
    try:
        with caplog.at_level("WARNING"):
            jobs = await scraper.fetch_jobs()
    finally:
        await scraper.aclose()

    assert len(jobs) == 1
    assert scraper.rejected_count == 2
    assert caplog.messages.count("applyguy.job.rejected") == 2


@pytest.mark.asyncio
async def test_injected_client_remains_caller_owned() -> None:
    client = client_for(feed_body())
    scraper = ApplyGuyScraper(client=client)
    await scraper.aclose()
    assert client.is_closed is False
    await client.aclose()


@pytest.mark.asyncio
async def test_owned_client_is_closed() -> None:
    scraper = ApplyGuyScraper(feed_url="https://example.test/jobs.json")
    await scraper.aclose()
    assert scraper._client.is_closed is True


class _MemoryDeduplicator:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def classify_and_update(
        self, *, base_hash: str, content_hash: str, is_closed: bool
    ) -> DeduplicationState:
        del is_closed
        previous = self.values.get(base_hash)
        self.values[base_hash] = content_hash
        return (
            DeduplicationState.NEW_ROLE
            if previous is None
            else (
                DeduplicationState.NO_OP
                if previous == content_hash
                else DeduplicationState.ROLE_UPDATED
            )
        )


@pytest.mark.asyncio
async def test_duplicate_rows_in_one_feed_are_coalesced() -> None:
    body = feed_body()
    body["jobs"] = [body["jobs"][0], dict(body["jobs"][0])]
    client = client_for(body)
    scraper = ApplyGuyScraper("internship", client=client)
    try:
        result = await ApplyGuyPipeline(scraper, _MemoryDeduplicator(), None).run()  # type: ignore[arg-type]
    finally:
        await scraper.aclose()
        await client.aclose()

    assert len(result.outcomes) == 1
    assert len(result.duplicates) == 1


def test_applyguy_uses_global_company_title_identity() -> None:
    assert generate_base_hash("Mercury", "Software Engineering Intern") == generate_base_hash(
        " mercury ", "Software_Engineering Intern"
    )
    assert generate_base_hash("Mercury", "Software Engineering Intern") != generate_base_hash(
        "Other Corp", "Software Engineering Intern"
    )
    assert generate_base_hash("Mercury", "Software Engineering Intern") != generate_base_hash(
        "Mercury", "Data Engineering Intern"
    )
