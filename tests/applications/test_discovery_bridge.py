"""Logical discovery identity bridges to a single durable application."""

import pytest
from app.db.models import ApplicationAnswer, ApplicationAttempt, DiscoveryEvent, JobPosting
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from app.services.hasher import generate_content_hash
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("bulk", [False, True])
async def test_duplicate_source_discovery_yields_one_application_opportunity(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    bulk: bool,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        for source in ("greenhouse", "applyguy", "simplify"):
            observation = application_job.model_copy(update={"location_source": source})
            if bulk:
                await repository.bulk_upsert_job_postings([observation])
            else:
                await repository.save_job_posting(observation)
        server = JobPingMCPServer(repository)
        queued = await server.jobs_get_next()
        assert queued is not None
        await server.application_start(queued["job_id"], queued["apply_url"])
        assert await server.jobs_get_next() is None
        for model in (JobPosting, ApplicationAttempt, DiscoveryEvent):
            assert await session.scalar(select(func.count()).select_from(model)) == 1


async def test_close_and_reopen_preserves_submitted_application_and_answers(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository)
        await server.application_start(posting.id, posting.apply_url)
        await server.application_save_answer(
            posting.id, question="Why?", answer="Reviewed", source="human"
        )
        await server.application_mark_ready(posting.id, posting.apply_url)
        await server.application_mark_submitted(posting.id, "https://example.com/thanks")
        for closed in (True, False):
            update = application_job.model_copy(
                update={
                    "is_closed": closed,
                    "content_hash": generate_content_hash(
                        application_job.base_hash,
                        str(application_job.apply_url),
                        application_job.location,
                        closed,
                    ),
                }
            )
            persisted = await repository.save_job_posting(update)
            assert persisted.id == posting.id
        assert await server.jobs_get_next() is None
        assert (await server.application_get_checkpoint(posting.id))["status"] == "submitted"
        assert await session.scalar(select(func.count()).select_from(ApplicationAttempt)) == 1
        assert await session.scalar(select(func.count()).select_from(ApplicationAnswer)) == 1
