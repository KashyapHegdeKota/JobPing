"""Integration tests for the direct ATS persistence pipeline."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest
from app.db.models import Base, JobPosting, StatusLog
from app.pipelines.ats_pipeline import ATSPipeline
from app.schemas.job import JobType, RawJobPayload
from app.scrapers.base import BaseScraper
from app.services.deduplicator import DeduplicationState
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class FakeScraper(BaseScraper):
    def __init__(
        self,
        source: str,
        company: str,
        rows: Sequence[RawJobPayload] = (),
        error: Exception | None = None,
    ) -> None:
        super().__init__(scraper_name=source, company=company, client=httpx.AsyncClient())
        self._rows = list(rows)
        self._error = error

    async def fetch_jobs(self) -> list[RawJobPayload]:
        if self._error is not None:
            raise self._error
        return self._rows


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


def row(
    *,
    source: str = "greenhouse",
    source_id: str = "1",
    url: str = "https://example.com/jobs/1",
    location: str = "New York",
    closed: bool = False,
) -> RawJobPayload:
    return RawJobPayload(
        source=source,
        source_id=source_id,
        company="Acme",
        title="Software Engineer Intern",
        apply_url=url,
        location=location,
        is_closed=closed,
    )


@pytest.fixture
async def sessions() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_update_closed_and_noop_are_persisted_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    scenarios = [
        (row(), DeduplicationState.NEW_ROLE),
        (row(location="Remote"), DeduplicationState.ROLE_UPDATED),
        (row(location="Remote", closed=True), DeduplicationState.ROLE_CLOSED),
        (row(location="Remote", closed=True), DeduplicationState.NO_OP),
    ]
    async with sessions() as session:
        for payload, expected in scenarios:
            scraper = FakeScraper(payload.source, "Acme", [payload])
            result = await ATSPipeline(
                [scraper], dedupe, session, season=2026, job_type=JobType.INTERNSHIP
            ).run()
            assert result.outcomes[0].state is expected
            await scraper.aclose()

        posting = (await session.scalars(select(JobPosting))).one()
        assert posting.location == "Remote"
        assert posting.is_closed is True
        assert await session.scalar(select(func.count()).select_from(StatusLog)) == 2


@pytest.mark.asyncio
async def test_duplicate_malformed_and_scraper_failure_are_reported(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = FakeScraper("greenhouse", "Acme", [row()])
    duplicate = FakeScraper("lever", "Acme", [row(source="lever", source_id="two")])
    malformed = FakeScraper(
        "lever",
        "Broken",
        [RawJobPayload(source="lever", company="Broken", title=None, apply_url=None)],
    )
    failed = FakeScraper("lever", "Down", error=RuntimeError("ATS unavailable"))
    async with sessions() as session:
        result = await ATSPipeline(
            [first, duplicate, malformed, failed],
            MemoryDeduplicator(),
            session,
            season=2027,
            job_type=JobType.NEW_GRAD,
        ).run()
        assert len(result.outcomes) == 1
        assert len(result.duplicates) == 1
        assert result.duplicates[0].source == "lever"
        assert len(result.rejected) == 1
        assert len(result.failures) == 1
        assert result.failures[0].error_type == "RuntimeError"
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
    for scraper in (first, duplicate, malformed, failed):
        await scraper.aclose()


@pytest.mark.asyncio
async def test_database_failure_rolls_back_and_is_not_hidden(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    scraper = FakeScraper("greenhouse", "Acme", [row()])
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        pipeline = ATSPipeline([scraper], dedupe, session, season=2026, job_type=JobType.INTERNSHIP)

        async def fail_bulk(jobs: object) -> list[JobPosting]:
            del jobs
            raise RuntimeError("database write failed")

        monkeypatch.setattr(pipeline._repository, "bulk_upsert_job_postings", fail_bulk)
        with pytest.raises(RuntimeError, match="database write failed"):
            await pipeline.run()
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0
        assert len(dedupe.values) == 1  # documented Redis-ahead limitation
    await scraper.aclose()
