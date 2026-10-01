"""Reset/retry retains audit answers without resetting a submitted application."""

import pytest
from app.applications.service import ApplicationService
from app.db.models import ApplicationAnswer, ApplicationStatus
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_review_reset_clears_metadata_and_preserves_answer_history(
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
        await server.application_mark_review_required(
            posting.id, review_question="Unknown?", review_reason="Ask user."
        )
        original = await repository.get_application_attempt(posting.id)
        assert original is not None
        reset = await server.service.reset_application(posting.id)
        assert reset.id == original.id and reset.status == ApplicationStatus.QUEUED
        assert reset.checkpoint_stage is None and reset.review_question is None
        assert reset.current_url is None and reset.attempt_count == 1
        started = await server.application_start(posting.id, posting.apply_url)
        assert started["attempt_count"] == 2
        assert len(list((await session.scalars(select(ApplicationAnswer))).all())) == 1


async def test_failed_attempt_retries_with_stable_identity_and_cleared_failure(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        service = ApplicationService(repository)
        original = await service.start_application(posting.id)
        await service.mark_failed(posting.id, "browser_error", "Browser closed.")
        restarted = await service.start_application(posting.id)
        assert restarted.id == original.id and restarted.attempt_count == 2
        assert restarted.failure_code is None and restarted.failure_message is None
        await service.mark_ready(posting.id)
        await service.mark_submitted(posting.id, "https://example.com/thanks")
        with pytest.raises(ValueError, match="cannot be reset"):
            await service.reset_application(posting.id)
