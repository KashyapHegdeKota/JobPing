"""Cross-source ApplyGuy identity and canonical URL integration coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from app.db.models import Base, JobPosting
from app.pipelines.applyguy_pipeline import ApplyGuyPipeline
from app.pipelines.ats_pipeline import ATSPipeline
from app.pipelines.simplify_pipeline import SimplifyPipeline
from app.schemas.job import JobType, RawJobPayload
from app.scrapers.applyguy import ApplyGuyScraper
from app.scrapers.base import BaseScraper
from app.scrapers.github_client import GitHubCommitDetail, GitHubFilePatch
from app.services.deduplicator import DeduplicationState
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class MemoryDeduplicator:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def classify_and_update(
        self, *, base_hash: str, content_hash: str, is_closed: bool
    ) -> DeduplicationState:
        previous = self.values.get(base_hash)
        self.values[base_hash] = content_hash
        if previous is None:
            return DeduplicationState.NEW_ROLE
        if previous == content_hash:
            return DeduplicationState.NO_OP
        return DeduplicationState.ROLE_CLOSED if is_closed else DeduplicationState.ROLE_UPDATED


class OneRowScraper(BaseScraper):
    def __init__(self, row: RawJobPayload) -> None:
        super().__init__(
            scraper_name=row.source, company=row.company or "Unknown", client=httpx.AsyncClient()
        )
        self.row = row

    async def fetch_jobs(self) -> list[RawJobPayload]:
        return [self.row]


def greenhouse_row() -> RawJobPayload:
    return RawJobPayload(
        source="greenhouse",
        source_id="greenhouse:mercury:6199367004",
        company="Mercury",
        title="Software Engineering Intern - Spring 2027",
        apply_url="https://boards.greenhouse.io/mercury/jobs/6199367004",
        location="Remote, U.S.",
    )


def applyguy_payload() -> dict[str, object]:
    return {
        "updatedAt": "2026-09-21T20:15:30.615Z",
        "jobs": [
            {
                "id": "mercury-1",
                "company": "Mercury",
                "title": "Software Engineering Intern - Spring 2027",
                "location": "Remote, U.S.",
                "posted": "2026-09-21",
                "url": "https://applyguy.ai/jobs?id=mercury-1",
                "listingUrl": "https://job-boards.greenhouse.io/mercury/jobs/6199367004",
            }
        ],
    }


@pytest.mark.asyncio
async def test_greenhouse_applyguy_and_simplify_share_one_posting() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    async with factory() as session:
        greenhouse = OneRowScraper(greenhouse_row())
        greenhouse_result = await ATSPipeline(
            [greenhouse], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
        ).run()
        await greenhouse.aclose()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, json=applyguy_payload())

        applyguy_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        applyguy = ApplyGuyScraper("internship", client=applyguy_client)
        applyguy_result = await ApplyGuyPipeline(applyguy, dedupe, session).run()
        repeat_result = await ApplyGuyPipeline(applyguy, dedupe, session).run()
        await applyguy.aclose()
        await applyguy_client.aclose()

        simplify = SimplifyPipeline(  # type: ignore[arg-type]
            _SimplifyGitHub(), dedupe, season=2027, job_type=JobType.INTERNSHIP
        )
        simplify_result = await simplify.process_commit("SimplifyJobs", "Summer2027-Internships")

        assert greenhouse_result.outcomes[0].state is DeduplicationState.NEW_ROLE
        assert applyguy_result.outcomes[0].state is DeduplicationState.NO_OP
        assert repeat_result.outcomes[0].state is DeduplicationState.NO_OP
        assert applyguy_result.outcomes[0].job.posted_at == datetime(2026, 9, 21, tzinfo=UTC)
        assert simplify_result.categorized(DeduplicationState.NO_OP)
        postings = (await session.scalars(select(JobPosting))).all()
        assert len(postings) == 1
        assert postings[0].apply_url == "https://job-boards.greenhouse.io/mercury/jobs/6199367004"
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_lever_and_applyguy_share_one_posting() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    lever = RawJobPayload(
        source="lever",
        source_id="lever-1",
        company="Acme",
        title="Data Engineer",
        apply_url="https://jobs.lever.co/acme/abc123?utm_source=lever",
        location="Remote",
    )
    body = {
        "updatedAt": "2026-09-21T20:15:30Z",
        "jobs": [
            {
                "id": "acme-1",
                "company": "Acme",
                "title": "Data Engineer",
                "location": "Remote",
                "posted": "2026-09-21",
                "url": "https://applyguy.ai/jobs/acme-1",
                "listingUrl": "https://jobs.lever.co/acme/abc123",
            }
        ],
    }
    async with factory() as session:
        direct = OneRowScraper(lever)
        first = await ATSPipeline(
            [direct], dedupe, session, season=2027, job_type=JobType.NEW_GRAD
        ).run()
        await direct.aclose()
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request, json=body)
            )
        )
        scraper = ApplyGuyScraper("new-grad", client=client)
        second = await ApplyGuyPipeline(scraper, dedupe, session).run()
        await scraper.aclose()
        await client.aclose()

        assert first.outcomes[0].state is DeduplicationState.NEW_ROLE
        assert second.outcomes[0].state is DeduplicationState.NO_OP
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
    await engine.dispose()


class _SimplifyGitHub:
    async def get_commit(self, *args: object, **kwargs: object) -> GitHubCommitDetail:
        del args, kwargs
        patch = (
            "+| Mercury | Software Engineering Intern - Spring 2027 | Remote, U.S. | "
            "[Apply](https://boards.greenhouse.io/mercury/jobs/6199367004?"
            "utm_source=Simplify) | Today |"
        )
        return GitHubCommitDetail(
            "simplify-sha",
            "roles",
            "https://github.com/SimplifyJobs/commit/simplify-sha",
            datetime(2026, 9, 21, tzinfo=UTC),
            (GitHubFilePatch("README.md", "modified", 1, 0, 1, patch),),
        )
