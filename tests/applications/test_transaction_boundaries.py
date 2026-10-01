"""Rollback and uniqueness protect durable application storage boundaries."""

import pytest
from app.db.models import ApplicationAnswer, ApplicationAttempt, ApplicationStatus
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_caller_rollback_removes_application_and_answers(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        posting = await DatabaseRepository(session).save_job_posting(application_job)
        job_id = posting.id
        await session.commit()
        async with session.begin():
            server = JobPingMCPServer(DatabaseRepository(session))
            await server.application_start(job_id, str(application_job.apply_url))
            await server.application_save_answer(
                job_id, question="Why?", answer="Reviewed", source="human"
            )
            await session.rollback()
    async with application_sessions() as fresh:
        assert await fresh.scalar(select(func.count()).select_from(ApplicationAttempt)) == 0
        assert await fresh.scalar(select(func.count()).select_from(ApplicationAnswer)) == 0
        assert await DatabaseRepository(fresh).get_job_by_id(job_id) is not None


async def test_database_unique_constraint_rejects_second_attempt(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        job_id = posting.id
        await repository.ensure_application_attempt(job_id)
        await session.commit()
    async with application_sessions() as second:
        second.add(ApplicationAttempt(job_id=job_id, status=ApplicationStatus.QUEUED))
        with pytest.raises(IntegrityError):
            await second.commit()
        await second.rollback()
    async with application_sessions() as fresh:
        assert await fresh.scalar(select(func.count()).select_from(ApplicationAttempt)) == 1
