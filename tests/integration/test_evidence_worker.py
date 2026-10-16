"""Worker commit/publication, partial observations, invalidation and resource contracts."""

from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import DiscoveryEvent, EvidenceCheck, EvidenceDocument
from app.db.repository import DatabaseRepository
from app.evidence.collect import CollectionError
from app.evidence.schemas import Document
from app.evidence.store import claim, enqueue, finish
from app.evidence.worker import EvidenceWorker, reviewed_employers, serve
from app.schemas.job import NormalizedJob
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration.test_evidence import seed


class FixtureCollector:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def collect(self, target: dict) -> list[Document]:
        document = Document(
            source_url=target["url"],
            final_url=target["url"],
            kind="posting",
            text="We accept OPT. Base salary USD 80000-120000 per year.",
            observed_at=datetime.now(UTC),
            role_identity=target,
            source="greenhouse",
            location=target["location"],
        )
        if self.fail:
            raise CollectionError("http_unavailable", [document])
        return [document]


class FixturePublisher:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def publish_job_event(self, *args: object, **kwargs: object) -> bool:
        self.messages.append(dict(kwargs))
        return True


@pytest.mark.parametrize("fail", [False, True])
async def test_worker_tick_partial_snapshots_and_post_commit_metadata(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    fail: bool,
) -> None:
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        identifier = occurrence.id
        events = await db.scalar(select(func.count()).select_from(DiscoveryEvent))
    publisher = FixturePublisher()
    worker = EvidenceWorker(application_sessions, FixtureCollector(fail=fail), publisher)
    await worker.tick()
    async with application_sessions() as db:
        check = await db.get(EvidenceCheck, identifier)
        assert check and check.status == ("partial" if fail else "complete")
        assert bool(check.verified_at) != fail
        assert await db.scalar(select(func.count()).select_from(EvidenceDocument)) == 1
        assert await db.scalar(select(func.count()).select_from(DiscoveryEvent)) == events
    assert len(publisher.messages) == (0 if fail else 1)


async def test_polling_cannot_oscillate_complete_projection_or_renew_unchanged_timestamp(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCE_WORKER_ENABLED", "true")
    observed = application_job.model_copy(
        update={"source": "greenhouse", "location": "Austin, TX, US"}
    )
    async with application_sessions() as db, db.begin():
        posting = await DatabaseRepository(db).save_job_posting(observed)
        job_id = posting.id
    worker = EvidenceWorker(application_sessions, FixtureCollector())
    await worker.tick()
    async with application_sessions() as db, db.begin():
        posting = await DatabaseRepository(db).save_job_posting(observed)
        assert posting.id == job_id
        check = await db.scalar(select(EvidenceCheck))
        assert check and check.status == "complete"
        assert posting.details["policies"]["opt"]["value"] == "allowed"


async def test_changed_source_revokes_browser_claim(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    now = datetime.now(UTC)
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        await enqueue(
            db,
            posting,
            occurrence,
            now,
            observation_source="greenhouse",
            observation_details={"policies": {}},
        )
        old = await claim(db, now)
        assert old
        await enqueue(
            db,
            posting,
            occurrence,
            now + timedelta(seconds=1),
            observation_source="greenhouse",
            observation_details={"policies": {"opt": "changed"}},
        )
        assert not await finish(db, *old, [], now + timedelta(seconds=2))
        assert await claim(db, now + timedelta(seconds=2))


async def test_worker_disabled_does_not_create_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("EVIDENCE_WORKER_ENABLED", raising=False)
    await serve(once=True)
    monkeypatch.delenv("EVIDENCE_EMPLOYERS_FILE", raising=False)
    assert reviewed_employers() == {}


async def test_metadata_rollback_never_publishes(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.evidence.worker as module

    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        original_details = posting.details
        await enqueue(db, posting, occurrence, datetime.now(UTC))
        selected = await claim(db, datetime.now(UTC))
        assert selected
        job_id = posting.id
    original_finish = module.finish

    async def fail_after_finish(*args: object, **kwargs: object) -> bool:
        await original_finish(*args, **kwargs)
        raise RuntimeError("Controlled transaction rollback")

    monkeypatch.setattr(module, "finish", fail_after_finish)
    publisher = FixturePublisher()
    with pytest.raises(RuntimeError, match="Controlled transaction rollback"):
        await EvidenceWorker(application_sessions, FixtureCollector(), publisher).process(selected)
    async with application_sessions() as db:
        posting = await db.get(module.JobPosting, job_id)
        check = await db.get(EvidenceCheck, selected[0])
        assert posting and posting.details == original_details
        assert check and check.status == "running" and check.verified_at is None
        assert await db.scalar(select(func.count()).select_from(EvidenceDocument)) == 0
    assert publisher.messages == []
