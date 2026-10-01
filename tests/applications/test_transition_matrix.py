"""An independent specification of every status pair, including self transitions."""

import pytest
from app.db.models import ApplicationStatus, VerificationType
from app.db.repository import DatabaseRepository
from app.schemas.job import NormalizedJob
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LEGAL_EDGES = {
    ("queued", "in_progress"),
    ("queued", "failed"),
    ("in_progress", "needs_verification"),
    ("in_progress", "needs_review"),
    ("in_progress", "ready_to_submit"),
    ("in_progress", "failed"),
    ("needs_verification", "in_progress"),
    ("needs_verification", "failed"),
    ("needs_review", "failed"),
    ("ready_to_submit", "submitted"),
    ("ready_to_submit", "failed"),
    ("failed", "in_progress"),
}


@pytest.mark.parametrize("before", list(ApplicationStatus))
@pytest.mark.parametrize("after", list(ApplicationStatus))
async def test_every_application_transition(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    before: ApplicationStatus,
    after: ApplicationStatus,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        attempt = await repository.ensure_application_attempt(posting.id)
        attempt.status = before
        await session.flush()
        if (before.value, after.value) in LEGAL_EDGES:
            changed = await repository.transition_application(
                posting.id,
                after,
                verification_type=VerificationType.CAPTCHA,
                confirmation_url="https://example.com/confirmation",
            )
            assert changed.id == attempt.id and changed.status == after
            assert changed.attempt_count == int(
                after == ApplicationStatus.IN_PROGRESS
                and before in {ApplicationStatus.QUEUED, ApplicationStatus.FAILED}
            )
            assert (changed.submitted_at is not None) == (after == ApplicationStatus.SUBMITTED)
        else:
            with pytest.raises(ValueError, match="invalid application transition"):
                await repository.transition_application(posting.id, after)
            assert attempt.status == before and attempt.attempt_count == 0
