"""Discovery eligibility, deterministic ordering and stable attempt identity."""

from datetime import UTC, datetime

import pytest
from app.db.models import ApplicationAttempt, ApplicationStatus
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from app.services.hasher import generate_base_hash
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_queue_request_is_repeatable_and_newest_id_breaks_timestamp_ties(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        first = await repository.save_job_posting(application_job)
        second = await repository.save_job_posting(
            application_job.model_copy(
                update={
                    "title": "Second role",
                    "base_hash": generate_base_hash("Fixture Co", "Second role"),
                }
            )
        )
        first.created_at = second.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        await session.flush()
        server = JobPingMCPServer(repository)
        for _ in range(3):
            result = await server.jobs_get_next()
            assert result is not None and result["job_id"] == second.id
            assert result["status"] == "queued"
        assert await session.scalar(select(func.count()).select_from(ApplicationAttempt)) == 1
        queue = await repository.list_application_queue()
        assert [attempt.job_id for attempt in queue] == [second.id, first.id]
        assert all(attempt.attempt_count == 0 for attempt in queue)


async def test_closed_job_and_missing_job_cannot_start_or_queue(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(
            application_job.model_copy(update={"is_closed": True})
        )
        server = JobPingMCPServer(repository)
        assert await server.jobs_get_next() is None
        for job_id, message in ((posting.id, "closed"), (9999, "not found")):
            with pytest.raises(ValueError, match=message):
                await server.application_start(job_id, application_job.apply_url)
        assert await session.scalar(select(func.count()).select_from(ApplicationAttempt)) == 0


@pytest.mark.parametrize(
    "status", [status for status in ApplicationStatus if status != ApplicationStatus.QUEUED]
)
async def test_queue_omits_every_active_and_terminal_status(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    status: ApplicationStatus,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        attempt = await repository.ensure_application_attempt(posting.id)
        attempt.status = status
        await session.flush()
        assert await repository.get_next_application() is None
        assert await repository.list_application_queue() == []
