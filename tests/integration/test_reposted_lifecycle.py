"""Regression coverage for occurrence creation and conservative repost classification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from app.db.models import (
    Base,
    DiscoveryEvent,
    EmailDelivery,
    JobMatch,
    JobOccurrence,
    JobPosting,
    JobSourceObservation,
    Subscriber,
)
from app.db.repository import DatabaseRepository
from app.notifications.worker import match_events
from app.pipelines.ats_pipeline import ATSPipeline
from app.pipelines.simplify_pipeline import SimplifyPipeline
from app.schemas.job import JobType, RawJobPayload
from app.scrapers.base import BaseScraper
from app.scrapers.github_client import GitHubCommitDetail, GitHubFilePatch
from app.services.deduplicator import DeduplicationState
from app.services.source_identity import stable_posting_identity
from sqlalchemy import delete, func, select
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
        super().__init__(scraper_name=row.source, company=row.company or "Acme")
        self.row = row

    async def fetch_jobs(self) -> list[RawJobPayload]:
        return [self.row]


def test_ats_identity_is_provider_and_tenant_scoped() -> None:
    acme = stable_posting_identity(
        source="applyguy",
        source_id="feed-record-1",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        payload={},
    )
    other_tenant = stable_posting_identity(
        source="applyguy",
        source_id="feed-record-2",
        apply_url="https://boards.greenhouse.io/other/jobs/123",
        payload={},
    )
    spoofed_host = stable_posting_identity(
        source="greenhouse",
        source_id="123",
        apply_url="https://acmegreenhouse.io/acme/jobs/123",
        payload={},
    )
    assert acme == ("greenhouse:acme", "123")
    assert other_tenant == ("greenhouse:other", "123")
    assert spoofed_host is None


def raw(
    *,
    source: str = "greenhouse",
    source_id: str | None = "greenhouse:acme:123",
    apply_url: str = "https://boards.greenhouse.io/acme/jobs/123",
    closed: bool = False,
    location: str = "Remote",
    posted: str | None = "2026-08-10",
) -> RawJobPayload:
    return RawJobPayload(
        source=source,
        source_id=source_id,
        company="Acme",
        title="Software Engineer Intern",
        apply_url=apply_url,
        location=location,
        is_closed=closed,
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
        payload={"posted": posted} if posted else {},
    )


async def ingest(
    row: RawJobPayload, session: AsyncSession, dedupe: MemoryDeduplicator
) -> DeduplicationState:
    scraper = OneRowScraper(row)
    try:
        result = await ATSPipeline(
            [scraper], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
        ).run()
        return result.outcomes[0].state
    finally:
        await scraper.aclose()


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_new_metadata_close_and_same_id_reopen_keep_one_occurrence(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        assert await ingest(raw(), session, dedupe) is DeduplicationState.NEW_ROLE
        assert (
            await ingest(raw(location="Remote, U.S."), session, dedupe)
            is DeduplicationState.ROLE_UPDATED
        )
        assert await ingest(raw(closed=True), session, dedupe) is DeduplicationState.ROLE_CLOSED
        assert await ingest(raw(), session, dedupe) is DeduplicationState.ROLE_UPDATED
        assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1


@pytest.mark.asyncio
async def test_bulk_upsert_does_not_duplicate_concurrent_first_discovery(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    dedupe = MemoryDeduplicator()
    row = raw()
    async with sessions() as session:
        await ingest(row, session, dedupe)
        pipeline = ATSPipeline([], dedupe, session, season=2027, job_type=JobType.INTERNSHIP)
        normalized = pipeline._normalize(row)
        repository = DatabaseRepository(session)

        async def missed_pre_read(_base_hashes: object) -> list[JobPosting]:
            # Model a worker whose initial SELECT ran before the winning insert,
            # but whose upsert and occurrence read run after that transaction.
            return []

        monkeypatch.setattr(repository, "_existing_postings_for_update", missed_pre_read)
        await repository.bulk_upsert_job_postings([normalized])

        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1


@pytest.mark.asyncio
async def test_bulk_upsert_rechecks_current_occurrence_before_creating_repost(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    original = raw()
    repost = raw(
        source="greenhouse",
        source_id="greenhouse:acme:19733",
        apply_url="https://boards.greenhouse.io/acme/jobs/19733",
        posted="2026-09-21",
    )
    async with sessions() as session:
        await ingest(original, session, dedupe)
        await ingest(original.model_copy(update={"is_closed": True}), session, dedupe)
        assert await ingest(repost, session, dedupe) is DeduplicationState.ROLE_REPOSTED

        pipeline = ATSPipeline([], dedupe, session, season=2027, job_type=JobType.INTERNSHIP)
        normalized = pipeline._normalize(repost).model_copy(update={"occurrence_kind": "reposted"})
        posting = await session.scalar(select(JobPosting))
        assert posting is not None

        # Model a transaction that read the prior closed posting before another
        # worker committed this matching repost occurrence.
        posting.is_closed = True
        await session.flush()
        await DatabaseRepository(session).bulk_upsert_job_postings([normalized])

        occurrences = list(await session.scalars(select(JobOccurrence).order_by(JobOccurrence.id)))
        events = list(await session.scalars(select(DiscoveryEvent).order_by(DiscoveryEvent.id)))
        assert [item.kind for item in occurrences] == ["discovered", "reposted"]
        assert [event.event_type for event in events] == ["discovered", "reposted"]
        assert await session.scalar(select(JobPosting.is_closed)) is False


@pytest.mark.asyncio
async def test_pipeline_stale_pre_read_reports_repost_only_for_winning_write(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    dedupe = MemoryDeduplicator()
    original = raw()
    repost = raw(
        source_id="greenhouse:acme:19733",
        apply_url="https://boards.greenhouse.io/acme/jobs/19733",
        posted="2026-09-21",
    )
    async with sessions() as session:
        await ingest(original, session, dedupe)
        await ingest(original.model_copy(update={"is_closed": True}), session, dedupe)
        pipeline_b = ATSPipeline(
            [OneRowScraper(repost)], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
        )
        pipeline_a = ATSPipeline(
            [OneRowScraper(repost)], dedupe, session, season=2027, job_type=JobType.INTERNSHIP
        )
        original_persist = pipeline_b._repository.bulk_upsert_job_postings_with_outcomes
        winner_results = []

        async def persist_after_worker_a(jobs: object) -> list[object]:
            # Pipeline B has already read the closed posting and occurrence. Let A
            # persist the same repost before B enters the lock-protected write.
            winner_results.append(await pipeline_a.run())
            return await original_persist(jobs)

        monkeypatch.setattr(
            pipeline_b._repository,
            "bulk_upsert_job_postings_with_outcomes",
            persist_after_worker_a,
        )
        try:
            loser = await pipeline_b.run()
        finally:
            await pipeline_a._scrapers[0].aclose()
            await pipeline_b._scrapers[0].aclose()

        assert winner_results[0].outcomes[0].state is DeduplicationState.ROLE_REPOSTED
        assert winner_results[0].outcomes[0].job.occurrence_kind == "reposted"
        assert loser.outcomes[0].state is DeduplicationState.NO_OP
        assert loser.outcomes[0].job.occurrence_kind is None
        assert (
            sum(
                outcome.state is DeduplicationState.ROLE_REPOSTED
                for result in (winner_results[0], loser)
                for outcome in result.outcomes
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 2
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 2
        posting = await session.scalar(select(JobPosting))
        assert posting is not None and not posting.is_closed
        assert dedupe.values[posting.base_hash] == posting.content_hash


@pytest.mark.asyncio
async def test_applyguy_first_then_greenhouse_and_simplify_coalesce_one_repost(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        old = raw()
        await ingest(old, session, dedupe)
        await ingest(raw(closed=True), session, dedupe)

        # ApplyGuy's feed-local id is ignored; its direct ATS URL gives strong identity.
        repost = raw(
            source="applyguy",
            source_id="feed-entry-19733",
            apply_url="https://job-boards.greenhouse.io/acme/jobs/19733",
            posted="2026-09-21",
        )
        assert await ingest(repost, session, dedupe) is DeduplicationState.ROLE_REPOSTED

        assert (
            await ingest(
                raw(
                    source_id="greenhouse:acme:19733",
                    apply_url="https://boards.greenhouse.io/acme/jobs/19733",
                    posted="2026-09-21",
                ),
                session,
                dedupe,
            )
            is DeduplicationState.NO_OP
        )

        simplify = SimplifyPipeline(
            _SimplifyGitHub(), dedupe, season=2027, job_type=JobType.INTERNSHIP
        )
        result = await simplify.process_commit("SimplifyJobs", "Summer2027-Internships")
        await SimplifyPipeline.persist_results(
            DatabaseRepository(session),
            (result,),
        )

        postings = list(await session.scalars(select(JobPosting)))
        occurrences = list(await session.scalars(select(JobOccurrence).order_by(JobOccurrence.id)))
        observations = list(await session.scalars(select(JobSourceObservation)))
        events = list(await session.scalars(select(DiscoveryEvent).order_by(DiscoveryEvent.id)))
        assert len(postings) == 1
        assert [item.kind for item in occurrences] == ["discovered", "reposted"]
        assert occurrences[1].previous_occurrence_id == occurrences[0].id
        assert len(events) == 2 and {event.event_type for event in events} == {
            "discovered",
            "reposted",
        }
        assert {item.source for item in observations} >= {
            "greenhouse",
            "applyguy",
            "simplify_github",
        }


@pytest.mark.asyncio
async def test_aggregator_churn_and_same_old_ats_id_do_not_create_repost(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        await ingest(raw(), session, dedupe)
        await ingest(raw(closed=True), session, dedupe)
        restored = raw(
            source="applyguy",
            source_id="different-feed-record",
            apply_url="https://applyguy.ai/jobs/acme-software-engineer",
            posted="2026-09-21",
        )
        assert await ingest(restored, session, dedupe) is DeduplicationState.NO_OP
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1
        posting = await session.scalar(select(JobPosting))
        occurrence = await session.scalar(select(JobOccurrence))
        assert posting is not None and posting.is_closed is True
        assert occurrence is not None and occurrence.closed_at is not None


@pytest.mark.asyncio
async def test_weak_open_keeps_closure_for_later_authoritative_repost(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        await ingest(raw(), session, dedupe)
        await ingest(raw(closed=True), session, dedupe)
        weak = raw(
            source="applyguy",
            source_id="feed-record-weak",
            apply_url="https://applyguy.ai/jobs/acme-software-engineer",
            posted="2026-09-21",
        )
        assert await ingest(weak, session, dedupe) is DeduplicationState.NO_OP
        posting = await session.scalar(select(JobPosting))
        occurrence = await session.scalar(select(JobOccurrence))
        assert posting is not None and posting.is_closed is True
        assert occurrence is not None and occurrence.closed_at is not None
        observation = await session.scalar(
            select(JobSourceObservation).where(JobSourceObservation.source_id == weak.source_id)
        )
        assert observation is not None
        assert observation.apply_url == weak.apply_url
        assert observation.posted_at.replace(tzinfo=UTC) == datetime(2026, 9, 21, tzinfo=UTC)
        assert dedupe.values[posting.base_hash] == posting.content_hash
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1

        repost = raw(
            source_id="greenhouse:acme:19733",
            apply_url="https://boards.greenhouse.io/acme/jobs/19733",
            posted="2026-09-22",
        )
        assert await ingest(repost, session, dedupe) is DeduplicationState.ROLE_REPOSTED
        occurrences = list(await session.scalars(select(JobOccurrence).order_by(JobOccurrence.id)))
        events = list(await session.scalars(select(DiscoveryEvent).order_by(DiscoveryEvent.id)))
        assert [item.kind for item in occurrences] == ["discovered", "reposted"]
        assert occurrences[-1].apply_url == "https://job-boards.greenhouse.io/acme/jobs/19733"
        assert [event.event_type for event in events] == ["discovered", "reposted"]
        user = Subscriber(
            id="repost-test-user",
            email="repost-test@example.com",
            verified=True,
            alerts=True,
            recap=False,
            job_types=["internship"],
            seasons=[2027],
            timezone="UTC",
            opted_at=datetime(2026, 9, 1, tzinfo=UTC),
            webhook_id="repost-test-webhook",
            connection_version="v1",
            unsubscribe_token="repost-test-unsubscribe",
        )
        session.add(user)
        await session.flush()
        await match_events(session, datetime(2026, 9, 22, tzinfo=UTC))
        await match_events(session, datetime(2026, 9, 22, tzinfo=UTC))
        repost_occurrence = occurrences[-1]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(JobMatch)
                .where(
                    JobMatch.subscriber_id == user.id,
                    JobMatch.occurrence_id == repost_occurrence.id,
                    JobMatch.event_type == "reposted",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EmailDelivery)
                .where(
                    EmailDelivery.subscriber_id == user.id,
                    EmailDelivery.kind == "alert",
                    EmailDelivery.occurrence_ids == [repost_occurrence.id],
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_single_repository_save_preserves_ambiguous_closure_and_allows_later_repost(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        pipeline = ATSPipeline(
            [], MemoryDeduplicator(), session, season=2027, job_type=JobType.INTERNSHIP
        )
        repository = DatabaseRepository(session)
        first = pipeline._normalize(raw())
        await repository.save_job_posting(first)
        closed = pipeline._normalize(raw(closed=True))
        await repository.save_job_posting(closed)

        weak = pipeline._normalize(
            raw(
                source="applyguy",
                source_id="feed-record-weak",
                apply_url="https://applyguy.ai/jobs/acme-software-engineer",
                posted="2026-09-21",
            )
        )
        await repository.save_job_posting(weak)
        posting = await session.scalar(select(JobPosting))
        occurrence = await session.scalar(select(JobOccurrence))
        assert posting is not None and posting.is_closed is True
        assert occurrence is not None and occurrence.closed_at is not None

        repost = pipeline._normalize(
            raw(
                source_id="greenhouse:acme:19733",
                apply_url="https://boards.greenhouse.io/acme/jobs/19733",
                posted="2026-09-22",
            )
        )
        await repository.save_job_posting(repost)
        occurrences = list(await session.scalars(select(JobOccurrence).order_by(JobOccurrence.id)))
        assert [item.kind for item in occurrences] == ["discovered", "reposted"]
        assert occurrences[-1].apply_url == "https://job-boards.greenhouse.io/acme/jobs/19733"


@pytest.mark.asyncio
async def test_same_direct_ats_id_with_new_date_is_an_update(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        await ingest(raw(), session, dedupe)
        await ingest(raw(closed=True), session, dedupe)
        reopened = raw(
            source_id="greenhouse:acme:123",
            apply_url="https://job-boards.greenhouse.io/acme/jobs/123",
            posted="2026-09-21",
        )
        assert await ingest(reopened, session, dedupe) is DeduplicationState.ROLE_UPDATED
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1


@pytest.mark.asyncio
async def test_different_ats_tenant_is_not_a_stable_id_repost(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        await ingest(raw(), session, dedupe)
        await ingest(raw(closed=True), session, dedupe)
        moved = raw(
            source_id="greenhouse:other:19733",
            apply_url="https://boards.greenhouse.io/other/jobs/19733",
            posted="2026-09-21",
        )
        assert await ingest(moved, session, dedupe) is DeduplicationState.NO_OP
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1
        posting = await session.scalar(select(JobPosting))
        assert posting is not None and posting.is_closed is True


@pytest.mark.asyncio
async def test_historical_ats_url_supplies_identity_without_backfilled_provenance(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        await ingest(raw(), session, dedupe)
        await ingest(raw(closed=True), session, dedupe)
        occurrence = await session.scalar(select(JobOccurrence))
        occurrence.identity_namespace = None
        occurrence.external_job_id = None
        occurrence.source = None
        occurrence.source_id = None
        occurrence.posted_at = None
        posting = await session.scalar(select(JobPosting))
        posting.posted_at = None
        await session.execute(
            delete(JobSourceObservation).where(JobSourceObservation.occurrence_id == occurrence.id)
        )

        result = await ingest(
            raw(
                source="greenhouse",
                source_id="greenhouse:acme:19733",
                apply_url="https://boards.greenhouse.io/acme/jobs/19733",
                posted=None,
            ),
            session,
            dedupe,
        )
        assert result is DeduplicationState.ROLE_REPOSTED
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 2


@pytest.mark.asyncio
async def test_url_and_new_date_fallback_requires_direct_ats_source(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    dedupe = MemoryDeduplicator()
    async with sessions() as session:
        old = raw(
            source="applyguy",
            source_id="old-feed-entry",
            apply_url="https://applyguy.ai/jobs/old",
        )
        await ingest(old, session, dedupe)
        await ingest(old.model_copy(update={"is_closed": True}), session, dedupe)
        aggregator_return = raw(
            source="applyguy",
            source_id="new-feed-entry",
            apply_url="https://applyguy.ai/jobs/new",
            posted="2026-09-21",
        )
        assert await ingest(aggregator_return, session, dedupe) is DeduplicationState.NO_OP
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 1
        posting = await session.scalar(select(JobPosting))
        assert posting is not None and posting.is_closed is True

    # A changed URL plus a newer posted date from a direct ATS source is the
    # conservative fallback when neither occurrence has a stable requisition ID.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    dedupe = MemoryDeduplicator()
    async with factory() as session:
        old = raw(
            source="workday",
            source_id=None,
            apply_url="https://careers.acme.example/job/old-role",
        )
        await ingest(old, session, dedupe)
        await ingest(old.model_copy(update={"is_closed": True}), session, dedupe)
        new = raw(
            source="workday",
            source_id=None,
            apply_url="https://careers.acme.example/job/new-role",
            posted="2026-09-21",
        )
        assert await ingest(new, session, dedupe) is DeduplicationState.ROLE_REPOSTED
        assert await session.scalar(select(func.count()).select_from(JobOccurrence)) == 2
    await engine.dispose()


class _SimplifyGitHub:
    async def get_commit(self, *args: object, **kwargs: object) -> GitHubCommitDetail:
        del args, kwargs
        line = (
            "+| Acme | Software Engineer Intern | Remote | "
            "[Apply](https://boards.greenhouse.io/acme/jobs/19733) | 0d |"
        )
        return GitHubCommitDetail(
            "simplify-repost-sha",
            "roles",
            "https://github.com/SimplifyJobs/commit/simplify-repost-sha",
            datetime(2026, 9, 22, tzinfo=UTC),
            (GitHubFilePatch("README.md", "modified", 1, 0, 1, line),),
        )
