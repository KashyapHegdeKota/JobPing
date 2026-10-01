"""Destroy service/session objects and resume using only durable state."""

import pytest
from app.db.models import ApplicationAnswer, ApplicationAttempt
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize(
    "pause", ["in_progress", "needs_verification", "needs_review", "ready_to_submit"]
)
async def test_application_survives_session_and_service_restart(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    pause: str,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        job_id = posting.id
        server = JobPingMCPServer(repository)
        await server.application_start(job_id, str(application_job.apply_url))
        await server.application_save_answer(
            job_id, question="First name", answer="Jane", source="candidate_profile"
        )
        await server.application_update_checkpoint(
            job_id, str(application_job.apply_url), "candidate_info"
        )
        if pause == "needs_verification":
            await server.application_mark_verification_required(
                job_id,
                verification_type="captcha",
                current_url=str(application_job.apply_url),
                instruction="Complete the challenge in the browser.",
            )
        elif pause == "needs_review":
            await server.application_mark_review_required(
                job_id, review_question="Expected compensation?", review_reason="No known fact."
            )
        elif pause == "ready_to_submit":
            await server.application_mark_ready(job_id, str(application_job.apply_url))
        expected = await server.application_get_checkpoint(job_id)
        await session.commit()
    async with application_sessions() as fresh:
        restarted = JobPingMCPServer(DatabaseRepository(fresh))
        checkpoint = await restarted.application_get_checkpoint(job_id)
        # SQLite drops timezone information on reload; compare the remaining business data.
        for timestamp in ("started_at", "updated_at", "submitted_at"):
            expected.pop(timestamp)
            checkpoint.pop(timestamp)
        assert checkpoint == expected
        assert checkpoint["status"] == pause and checkpoint["attempt_count"] == 1
        assert checkpoint["confirmation_url"] is None
        assert await fresh.scalar(select(func.count()).select_from(ApplicationAttempt)) == 1
        answers = list((await fresh.scalars(select(ApplicationAnswer))).all())
        assert len(answers) == 1 and answers[0].answer == "Jane"
        if pause == "needs_verification":
            resumed = await restarted.application_mark_verification_complete(
                job_id, str(application_job.apply_url)
            )
            assert resumed["attempt_count"] == 1 and resumed["verification_type"] is None
            await restarted.application_mark_ready(job_id, str(application_job.apply_url))
        elif pause == "in_progress":
            await restarted.application_mark_ready(job_id, str(application_job.apply_url))
        assert (await restarted.application_get_checkpoint(job_id))["submitted_at"] is None


async def test_discovery_to_mcp_ready_without_browser_interaction(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository)
        next_job = await server.jobs_get_next()
        assert next_job is not None and next_job["job_id"] == posting.id
        assert next_job["status"] == "queued"
        await server.application_start(posting.id, next_job["apply_url"])
        for stage in (
            "application_form",
            "candidate_info",
            "resume_uploaded",
            "custom_questions",
            "review",
        ):
            updated = await server.application_update_checkpoint(
                posting.id, posting.apply_url, stage
            )
            assert updated["status"] == "in_progress"
        ready = await server.application_mark_ready(posting.id, posting.apply_url)
        assert ready["status"] == "ready_to_submit" and ready["submitted_at"] is None
        await session.commit()
