"""Answers persist audit facts and reject malformed or verification values."""

import pytest
from app.db.models import ApplicationAnswer
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize(
    "changes",
    [
        {"question": " "},
        {"answer": " "},
        {"source": " "},
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"question": "Enter verification code", "answer": "123456"},
        {"question": "Authenticator seed", "answer": "secret"},
        {"answer": "one time password"},
    ],
)
async def test_rejected_answer_creates_no_record(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    changes: dict[str, object],
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository)
        args = {
            "question": "Why this role?",
            "answer": "Reviewed answer",
            "source": "human",
            **changes,
        }
        with pytest.raises(ValueError):
            await server.application_save_answer(posting.id, **args)
        assert await session.scalar(select(func.count()).select_from(ApplicationAnswer)) == 0
        assert await repository.get_application_attempt(posting.id) is None


async def test_answer_audit_survives_new_session_and_response_omits_value(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        result = await JobPingMCPServer(repository).application_save_answer(
            posting.id,
            question="  Why this role?  ",
            answer="  Reviewed answer  ",
            source="human",
            generated=False,
            confidence=1.0,
        )
        assert "answer" not in result
        await session.commit()
    async with application_sessions() as fresh:
        answer = (await fresh.scalars(select(ApplicationAnswer))).one()
        assert answer.answer == "Reviewed answer" and answer.normalized_question == "why this role"
        assert answer.source == "human" and not answer.generated and answer.confidence == 1.0
