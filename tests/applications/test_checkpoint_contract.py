"""Checkpoint metadata must respect active lifecycle states."""

import pytest
from app.db.models import ApplicationStatus, CheckpointStage
from app.db.repository import DatabaseRepository
from app.schemas.job import NormalizedJob
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

COMPATIBLE = {
    "in_progress": {
        "opened",
        "application_form",
        "candidate_info",
        "resume_uploaded",
        "custom_questions",
        "verification",
        "review",
        "ready_to_submit",
    },
    "needs_verification": {"verification"},
    "needs_review": {"review"},
    "ready_to_submit": {"ready_to_submit"},
}


@pytest.mark.parametrize("status", list(ApplicationStatus))
@pytest.mark.parametrize("stage", list(CheckpointStage))
async def test_checkpoint_status_stage_matrix(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    status: ApplicationStatus,
    stage: CheckpointStage,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        attempt = await repository.ensure_application_attempt(posting.id)
        attempt.status = status
        await session.flush()
        if stage.value in COMPATIBLE.get(status.value, set()):
            await repository.update_application_checkpoint(posting.id, stage=stage)
            assert attempt.checkpoint_stage == stage.value and attempt.status == status
        else:
            with pytest.raises(ValueError, match="active application|inconsistent"):
                await repository.update_application_checkpoint(posting.id, stage=stage)
            assert attempt.checkpoint_stage is None


async def test_invalid_checkpoint_inputs_leave_state_unchanged(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        with pytest.raises(ValueError, match="must be started"):
            await repository.update_application_checkpoint(posting.id, stage="opened")
        await repository.transition_application(posting.id, ApplicationStatus.IN_PROGRESS)
        for kwargs in ({"stage": "invented"}, {"current_url": "  "}):
            with pytest.raises(ValueError):
                await repository.update_application_checkpoint(posting.id, **kwargs)
        checkpoint = await repository.get_application_attempt(posting.id)
        assert checkpoint is not None and checkpoint.current_url is None
