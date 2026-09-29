"""Cross-source ApplyGuy identity and canonical URL integration coverage."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from app.db.models import Base, DiscoveryEvent, JobPosting
from app.db.repository import DatabaseRepository
from app.pipelines.applyguy_pipeline import ApplyGuyPipeline
from app.pipelines.ats_pipeline import ATSPipeline
from app.pipelines.simplify_pipeline import SimplifyPipeline
from app.schemas.job import JobType, RawJobPayload
from app.scrapers.applyguy import ApplyGuyScraper
from app.scrapers.base import BaseScraper
from app.scrapers.github_client import GitHubCommitDetail, GitHubFilePatch
from app.services.deduplicator import DeduplicationState
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


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
        super().__init__(scraper_name=row.source, company=row.company or "Unknown")
        self.row = row

    async def fetch_jobs(self) -> list[RawJobPayload]:
        return [self.row]


def greenhouse_row(*, location: str = "Remote, U.S.") -> RawJobPayload:
    return RawJobPayload(
        source="greenhouse",
        source_id="greenhouse:mercury:6199367004",
        company="Mercury",
        title="Software Engineering Intern - Spring 2027",
        apply_url="https://boards.greenhouse.io/mercury/jobs/6199367004",
        location=location,
    )


def applyguy_payload(*, location: str = "Remote, U.S.") -> dict[str, object]:
    return {
        "updatedAt": "2026-09-21T20:15:30.615Z",
        "jobs": [
            {
                "id": "mercury-1",
                "company": "Mercury",
                "title": "Software Engineering Intern - Spring 2027",
                "location": location,
                "posted": "2026-09-21",
                "url": "https://applyguy.ai/jobs?id=mercury-1",
                "listingUrl": "https://job-boards.greenhouse.io/mercury/jobs/6199367004",
            }
        ],
    }


def applyguy_fallback_payload() -> dict[str, object]:
    payload = applyguy_payload()
    job = payload["jobs"][0]
    assert isinstance(job, dict)
    job.pop("listingUrl")
    return payload


async def run_direct(dedupe: MemoryDeduplicator, session: AsyncSession) -> DeduplicationState:
    scraper = OneRowScraper(greenhouse_row())
    try:
        result = await ATSPipeline(
            [scraper], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
        ).run()
        return result.outcomes[0].state
    finally:
        await scraper.aclose()


async def run_location_source(
    source: str,
    location: str,
    dedupe: MemoryDeduplicator,
    session: AsyncSession,
) -> DeduplicationState:
    if source == "greenhouse":
        row = greenhouse_row(location=location)
        scraper = OneRowScraper(row)
        try:
            result = await ATSPipeline(
                [scraper], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
            ).run()
            return result.outcomes[0].state
        finally:
            await scraper.aclose()
    body = applyguy_payload(location=location)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, json=body)
        )
    )
    scraper = ApplyGuyScraper("internship", client=client)
    try:
        result = await ApplyGuyPipeline(scraper, dedupe, session).run()
        return result.outcomes[0].state
    finally:
        await scraper.aclose()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_source", "second_source", "expected"),
    [
        (
            "applyguy_internships",
            "greenhouse",
            [DeduplicationState.NEW_ROLE, DeduplicationState.ROLE_UPDATED],
        ),
        (
            "greenhouse",
            "applyguy_internships",
            [DeduplicationState.NEW_ROLE, DeduplicationState.NO_OP],
        ),
    ],
)
async def test_location_authority_stabilizes_remote_labels_in_both_orders(
    first_source: str,
    second_source: str,
    expected: list[DeduplicationState],
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    async with factory() as session:
        states = [
            await run_location_source(
                first_source,
                "Remote" if first_source == "applyguy_internships" else "Remote, U.S.",
                dedupe,
                session,
            ),
            await run_location_source(
                second_source,
                "Remote" if second_source == "applyguy_internships" else "Remote, U.S.",
                dedupe,
                session,
            ),
            await run_location_source(
                first_source,
                "Remote" if first_source == "applyguy_internships" else "Remote, U.S.",
                dedupe,
                session,
            ),
        ]
        assert states == [*expected, DeduplicationState.NO_OP]
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.location == "Remote, U.S."
        assert posting.location_source == "greenhouse"
        assert dedupe.values[posting.base_hash] == posting.content_hash
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_location_source_can_correct_a_genuine_city_change() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    async with factory() as session:
        first = await run_location_source("greenhouse", "Austin, TX", dedupe, session)
        second = await run_location_source("greenhouse", "Seattle, WA", dedupe, session)
        posting = (await session.scalars(select(JobPosting))).one()
        assert first is DeduplicationState.NEW_ROLE
        assert second is DeduplicationState.ROLE_UPDATED
        assert posting.location == "Seattle, WA"
        assert posting.location_source == "greenhouse"
        assert dedupe.values[posting.base_hash] == posting.content_hash
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("greenhouse_first", [False, True])
async def test_duplicate_rows_coalesce_by_location_authority(greenhouse_first: bool) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    async with factory() as session:
        greenhouse = greenhouse_row(location="Remote, U.S.")
        applyguy = greenhouse.model_copy(
            update={"source": "applyguy_internships", "location": "Remote"}
        )
        rows = [greenhouse, applyguy] if greenhouse_first else [applyguy, greenhouse]
        scrapers = [OneRowScraper(row) for row in rows]
        try:
            result = await ATSPipeline(
                scrapers, dedupe, session, season=2027, job_type=JobType.INTERNSHIP
            ).run()
        finally:
            for scraper in scrapers:
                await scraper.aclose()

        assert len(result.outcomes) == 1
        assert len(result.duplicates) == 1
        assert result.outcomes[0].job.location == "Remote, U.S."
        assert result.outcomes[0].job.location_source == "greenhouse"
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.location == "Remote, U.S."
        assert posting.location_source == "greenhouse"
        assert dedupe.values[posting.base_hash] == posting.content_hash
    await engine.dispose()


@pytest.mark.asyncio
async def test_simplify_uses_effective_persisted_location_and_url_before_redis() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    async with factory() as session:
        direct = OneRowScraper(greenhouse_row(location="Remote, U.S."))
        initial = await ATSPipeline(
            [direct], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
        ).run()
        await direct.aclose()
        assert initial.outcomes[0].state is DeduplicationState.NEW_ROLE

        repository = DatabaseRepository(session)
        simplify = SimplifyPipeline(
            _SimplifyGitHub(location="Remote", apply_url="https://applyguy.ai/jobs?id=mercury-1"),
            dedupe,  # type: ignore[arg-type]
            season=2027,
            job_type=JobType.INTERNSHIP,
            repository=repository,
        )
        result = await simplify.process_commit("SimplifyJobs", "Summer2027-Internships")
        assert result.categorized(DeduplicationState.NO_OP)
        await SimplifyPipeline.persist_results(repository, (result,))
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.location == "Remote, U.S."
        assert posting.location_source == "greenhouse"
        assert posting.apply_url == "https://job-boards.greenhouse.io/mercury/jobs/6199367004"
        assert dedupe.values[posting.base_hash] == posting.content_hash
    await engine.dispose()


async def run_applyguy_fallback(
    dedupe: MemoryDeduplicator, session: AsyncSession
) -> DeduplicationState:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, json=applyguy_fallback_payload())
        )
    )
    scraper = ApplyGuyScraper("internship", client=client)
    try:
        result = await ApplyGuyPipeline(scraper, dedupe, session).run()
        return result.outcomes[0].state
    finally:
        await scraper.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_direct_and_applyguy_fallback_true_alternation_is_stable() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()

    async with factory() as session:
        states = [
            await run_direct(dedupe, session),
            await run_applyguy_fallback(dedupe, session),
            await run_direct(dedupe, session),
            await run_applyguy_fallback(dedupe, session),
        ]

        assert states == [
            DeduplicationState.NEW_ROLE,
            DeduplicationState.NO_OP,
            DeduplicationState.NO_OP,
            DeduplicationState.NO_OP,
        ]
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.apply_url == "https://job-boards.greenhouse.io/mercury/jobs/6199367004"
        assert dedupe.values[posting.base_hash] == posting.content_hash
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_applyguy_fallback_then_direct_improves_once_without_ping_pong() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()

    async with factory() as session:
        states = [
            await run_applyguy_fallback(dedupe, session),
            await run_direct(dedupe, session),
            await run_applyguy_fallback(dedupe, session),
            await run_direct(dedupe, session),
        ]

        assert states == [
            DeduplicationState.NEW_ROLE,
            DeduplicationState.ROLE_UPDATED,
            DeduplicationState.NO_OP,
            DeduplicationState.NO_OP,
        ]
        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.apply_url == "https://job-boards.greenhouse.io/mercury/jobs/6199367004"
        assert dedupe.values[posting.base_hash] == posting.content_hash
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1
    await engine.dispose()


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
    def __init__(
        self,
        *,
        location: str = "Remote, U.S.",
        apply_url: str = "https://boards.greenhouse.io/mercury/jobs/6199367004?utm_source=Simplify",
    ) -> None:
        self.location = location
        self.apply_url = apply_url

    async def get_commit(self, *args: object, **kwargs: object) -> GitHubCommitDetail:
        del args, kwargs
        patch = (
            "+| Mercury | Software Engineering Intern - Spring 2027 | "
            f"{self.location} | [Apply]({self.apply_url}) | Today |"
        )
        return GitHubCommitDetail(
            "simplify-sha",
            "roles",
            "https://github.com/SimplifyJobs/commit/simplify-sha",
            datetime(2026, 9, 21, tzinfo=UTC),
            (GitHubFilePatch("README.md", "modified", 1, 0, 1, patch),),
        )
