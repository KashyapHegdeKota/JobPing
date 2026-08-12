"""Tests for the job discovery API query behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from app.api.v1.jobs import list_jobs
from app.db.models import Base, Company, JobPosting, JobType
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed_jobs(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    google = Company(name="Google", domain="google.com", created_at=now)
    meta = Company(name="Meta", domain="meta.com", created_at=now)
    session.add_all([google, meta])
    await session.flush()
    session.add_all(
        [
            JobPosting(
                company_id=google.id,
                title="Software Engineering Intern",
                base_hash="a" * 64,
                content_hash="1" * 64,
                apply_url="https://google.com/intern",
                location="New York, NY",
                season=2027,
                job_type=JobType.INTERNSHIP,
                is_closed=False,
                created_at=now,
                updated_at=now,
            ),
            JobPosting(
                company_id=meta.id,
                title="Software Engineer, New Grad",
                base_hash="b" * 64,
                content_hash="2" * 64,
                apply_url="https://meta.com/new-grad",
                location="Menlo Park, CA",
                season=2027,
                job_type=JobType.NEW_GRAD,
                is_closed=True,
                created_at=now - timedelta(minutes=1),
                updated_at=now,
            ),
            JobPosting(
                company_id=google.id,
                title="Data Science Intern",
                base_hash="c" * 64,
                content_hash="3" * 64,
                apply_url="https://google.com/data",
                location="Remote",
                season=2026,
                job_type=JobType.INTERNSHIP,
                is_closed=False,
                created_at=now - timedelta(minutes=2),
                updated_at=now,
            ),
        ]
    )
    await session.commit()


async def test_list_jobs_paginates_with_deterministic_newest_first_order(
    session: AsyncSession,
) -> None:
    await _seed_jobs(session)

    first_page = await list_jobs(
        session, search=None, company=None, active=None, page=1, page_size=2
    )
    second_page = await list_jobs(
        session, search=None, company=None, active=None, page=2, page_size=2
    )

    assert first_page.total == 3
    assert first_page.total_pages == 2
    assert [job.title for job in first_page.items] == [
        "Software Engineering Intern",
        "Software Engineer, New Grad",
    ]
    assert [job.title for job in second_page.items] == ["Data Science Intern"]


@pytest.mark.parametrize(
    ("search", "expected_title"),
    [
        ("data science", "Data Science Intern"),
        ("mEtA", "Software Engineer, New Grad"),
        ("%", None),
    ],
)
async def test_search_matches_title_or_company_and_escapes_wildcards(
    session: AsyncSession,
    search: str,
    expected_title: str | None,
) -> None:
    await _seed_jobs(session)

    result = await list_jobs(
        session, search=search, company=None, active=None, page=1, page_size=20
    )

    assert [job.title for job in result.items] == (
        [expected_title] if expected_title is not None else []
    )


async def test_company_and_active_filters_compose(session: AsyncSession) -> None:
    await _seed_jobs(session)

    active_google = await list_jobs(
        session,
        search=None,
        company="  GOOGLE  ",
        active=True,
        page=1,
        page_size=20,
    )
    closed_google = await list_jobs(
        session,
        search=None,
        company="Google",
        active=False,
        page=1,
        page_size=20,
    )

    assert active_google.total == 2
    assert all(not job.is_closed for job in active_google.items)
    assert closed_google.total == 0
    assert closed_google.items == []
