"""Real PostgreSQL predicates, queue ownership and migration isolation."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from app.api.deps import get_db
from app.db.models import Base, EvidenceCheck
from app.evidence.store import claim, enqueue
from app.main import create_app
from app.notifications.security import Identity
from app.request_limits.dependencies import limited_identity
from app.schemas.job import NormalizedJob
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from tests.integration.test_evidence import seed
from tests.integration.test_evidence import (
    test_policy_location_and_target_bucket_parity as policy_parity,
)
from tests.integration.test_evidence import test_sql_python_bucket_parity as parity

pytestmark = pytest.mark.service_integration


@pytest.mark.parametrize(
    "text,status,age,expected",
    [
        ("Base salary USD 80000-120000 per year", "complete", 0, "matches"),
        ("Base salary USD 80000-90000 per year", "complete", 0, "excluded"),
        ("Base salary starting at USD 80000 per year", "complete", 0, "review"),
        ("Base salary USD 80000-120000 per year", "complete", 25, "review"),
    ],
)
async def test_postgres_screening(
    postgres_engine: AsyncEngine,
    application_job: NormalizedJob,
    text: str,
    status: str,
    age: int,
    expected: str,
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    await parity(sessions, application_job, text, status, age, expected)


async def test_postgres_claim_has_one_owner(
    postgres_engine: AsyncEngine, application_job: NormalizedJob
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        await enqueue(db, posting, occurrence, now)

    async def take() -> tuple | None:
        async with sessions() as db, db.begin():
            return await claim(db, now)

    results = await asyncio.gather(take(), take())
    assert sum(value is not None for value in results) == 1


async def test_postgres_profile_replay_and_refresh_coalescing(
    postgres_engine: AsyncEngine, application_job: NormalizedJob
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with sessions() as db, db.begin():
        posting, _ = await seed(db, application_job)
        job_id = posting.id
    app = create_app()

    async def database() -> AsyncIterator[AsyncSession]:
        async with sessions() as db, db.begin():
            yield db

    async def who() -> Identity:
        return Identity("fixture", "fixture@example.com", True)

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[limited_identity] = who
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        replies = await asyncio.gather(
            *[
                client.put("/api/v1/me/screening-profile", json={"authorization": "opt"})
                for _ in range(3)
            ]
        )
        assert all(reply.status_code == 200 and reply.json()["revision"] == 1 for reply in replies)
        replies = await asyncio.gather(
            *[client.post(f"/api/v1/me/jobs/{job_id}/evidence-check") for _ in range(3)]
        )
        assert all(reply.status_code == 202 for reply in replies)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(EvidenceCheck)) == 1


@pytest.mark.parametrize(
    "text,requirements,location,title_changed,expected",
    [
        (
            "We accept OPT. Unless exempt, candidates must be US citizens.",
            {"authorization": "opt"},
            "Austin, TX, US",
            False,
            "review",
        ),
        (
            "We accept OPT. Candidates must be US citizens.",
            {"authorization": "opt"},
            "Austin, TX, US",
            False,
            "excluded",
        ),
        ("We accept OPT.", {"authorization": "opt"}, "Toronto, Canada", False, "review"),
        ("We accept OPT.", {"authorization": "opt"}, "Austin, TX, US", True, "review"),
    ],
)
async def test_postgres_policy_target_parity(
    postgres_engine: AsyncEngine,
    application_job: NormalizedJob,
    text: str,
    requirements: dict,
    location: str,
    title_changed: bool,
    expected: str,
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await policy_parity(
        async_sessionmaker(postgres_engine, expire_on_commit=False),
        application_job,
        text,
        requirements,
        location,
        title_changed,
        expected,
    )
