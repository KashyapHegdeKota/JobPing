"""Async integration tests for the SQLAlchemy repository boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from app.db.models import Base, Company, JobPosting, StatusLog
from app.db.repository import DatabaseRepository
from app.schemas.job import NormalizedJob
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def make_job(**changes: object) -> NormalizedJob:
    values: dict[str, object] = {
        "company_name": "Acme",
        "title": "Software Engineer Intern",
        "base_hash": "a" * 64,
        "content_hash": "b" * 64,
        "apply_url": "https://example.com/jobs/1",
        "location": "New York, NY",
        "season": 2027,
        "job_type": "internship",
        "is_closed": False,
    }
    values.update(changes)
    return NormalizedJob.model_validate(values)


async def test_company_upsert_is_idempotent_and_preserves_known_domain(
    session: AsyncSession,
) -> None:
    repository = DatabaseRepository(session)
    first = await repository.upsert_company(" Acme ", "ACME.COM")
    second = await repository.upsert_company("Acme")

    assert first.id == second.id
    assert second.domain == "acme.com"
    assert await session.scalar(select(func.count()).select_from(Company)) == 1


async def test_job_insert_then_update_preserves_identity_and_creation_time(
    session: AsyncSession,
) -> None:
    repository = DatabaseRepository(session)
    created = datetime(2026, 8, 11, tzinfo=UTC)
    job = await repository.save_job_posting(make_job(created_at=created))
    original_id = job.id
    original_created_at = job.created_at

    updated = await repository.save_job_posting(
        make_job(content_hash="c" * 64, location="Remote", is_closed=True)
    )

    assert updated.id == original_id
    assert updated.created_at == original_created_at
    assert updated.content_hash == "c" * 64
    assert updated.location == "Remote"
    assert updated.is_closed is True
    assert updated.updated_at > created
    assert await session.scalar(select(func.count()).select_from(JobPosting)) == 1


async def test_unchanged_job_does_not_advance_updated_at(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    job = await repository.save_job_posting(make_job())
    previous_updated_at = job.updated_at

    same_job = await repository.save_job_posting(make_job())

    assert same_job.updated_at == previous_updated_at


async def test_job_lookup_by_base_hash_is_owned_by_repository(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    saved = await repository.save_job_posting(make_job())

    found = await repository.get_job_posting_by_base_hash("  " + ("A" * 64) + "  ")

    assert found is saved
    assert await repository.get_job_posting_by_base_hash("f" * 64) is None
    with pytest.raises(ValueError, match="must not be empty"):
        await repository.get_job_posting_by_base_hash("  ")


async def test_bulk_upsert_persists_multiple_jobs_and_state_transitions(
    session: AsyncSession,
) -> None:
    repository = DatabaseRepository(session)
    second = make_job(
        title="Data Science Intern",
        base_hash="c" * 64,
        content_hash="d" * 64,
        apply_url="https://example.com/jobs/2",
    )

    inserted = await repository.bulk_upsert_job_postings([make_job(), second])
    updated = await repository.bulk_upsert_job_postings(
        [make_job(content_hash="e" * 64, location="Remote", is_closed=True), second]
    )

    assert [posting.base_hash for posting in inserted] == ["a" * 64, "c" * 64]
    assert updated[0].location == "Remote"
    assert updated[0].is_closed is True
    assert await session.scalar(select(func.count()).select_from(Company)) == 1
    assert await session.scalar(select(func.count()).select_from(JobPosting)) == 2
    logs = (await session.scalars(select(StatusLog).order_by(StatusLog.id))).all()
    assert [(log.previous_state, log.new_state) for log in logs] == [
        (None, "OPEN"),
        (None, "OPEN"),
        ("OPEN", "CLOSED"),
    ]


async def test_bulk_upsert_rejects_duplicate_identities(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    with pytest.raises(ValueError, match="unique base hashes"):
        await repository.bulk_upsert_job_postings([make_job(), make_job()])


async def test_status_log_records_only_actual_transition(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    job = await repository.save_job_posting(make_job())
    log = await repository.log_status_change(job.id, "NEW_ROLE", "ROLE_CLOSED")

    assert log.job_id == job.id
    assert log.previous_state == "NEW_ROLE"
    assert log.new_state == "ROLE_CLOSED"
    assert await session.scalar(select(func.count()).select_from(StatusLog)) == 1
    with pytest.raises(ValueError, match="actual state transition"):
        await repository.log_status_change(job.id, "NO_OP", "NO_OP")


async def test_caller_owned_transaction_composes_and_rolls_back(
    session: AsyncSession,
) -> None:
    repository = DatabaseRepository(session)
    with pytest.raises(RuntimeError, match="abort"):
        async with session.begin():
            company = await repository.upsert_company("Rollback Corp", "rollback.test")
            job = await repository.save_job_posting(
                make_job(company_id=company.id, company_name=None)
            )
            await repository.log_status_change(job.id, None, "NEW_ROLE")
            raise RuntimeError("abort")

    assert await session.scalar(select(func.count()).select_from(Company)) == 0
    assert await session.scalar(select(func.count()).select_from(JobPosting)) == 0
    assert await session.scalar(select(func.count()).select_from(StatusLog)) == 0


async def test_explicit_transaction_composes_all_writes(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    async with session.begin():
        company = await repository.upsert_company("Atomic Corp")
        job = await repository.save_job_posting(make_job(company_id=company.id, company_name=None))
        log = await repository.log_status_change(job.id, None, "NEW_ROLE")

    assert company.id and job.id and log.id


async def test_missing_job_rejected(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        await DatabaseRepository(session).log_status_change(999, None, "NEW_ROLE")
