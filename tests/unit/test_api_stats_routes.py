"""Tests for company and aggregate analytics routes."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from app.api.deps import get_db
from app.api.v1.companies import router as companies_router
from app.api.v1.stats import router as stats_router
from app.db.models import Base, Company, JobPosting, JobType
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """Build an isolated ASGI app backed by in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(companies_router)
    app.include_router(stats_router)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.session_factory = session_factory  # type: ignore[attr-defined]
        yield client
    await engine.dispose()


async def _seed_jobs(client: httpx.AsyncClient) -> None:
    session_factory: async_sessionmaker[AsyncSession] = client.session_factory  # type: ignore[attr-defined]
    async with session_factory.begin() as session:
        zeta = Company(name="Zeta", domain="zeta.example")
        alpha = Company(name="alpha", domain="alpha.example")
        session.add_all([zeta, alpha])
        await session.flush()
        session.add_all(
            [
                JobPosting(
                    company_id=zeta.id,
                    title="Software Intern",
                    base_hash="a" * 64,
                    content_hash="b" * 64,
                    apply_url="https://zeta.example/intern",
                    location="New York, NY",
                    season=2027,
                    job_type=JobType.INTERNSHIP,
                    is_closed=False,
                ),
                JobPosting(
                    company_id=alpha.id,
                    title="New Grad Engineer",
                    base_hash="c" * 64,
                    content_hash="d" * 64,
                    apply_url="https://alpha.example/grad",
                    location="Remote",
                    season=2027,
                    job_type=JobType.NEW_GRAD,
                    is_closed=True,
                ),
            ]
        )


@pytest.mark.asyncio
async def test_companies_are_stably_sorted(api_client: httpx.AsyncClient) -> None:
    await _seed_jobs(api_client)

    response = await api_client.get("/companies")

    assert response.status_code == 200
    assert [company["name"] for company in response.json()] == ["alpha", "Zeta"]


@pytest.mark.asyncio
async def test_stats_report_lifecycle_and_job_type_counts(api_client: httpx.AsyncClient) -> None:
    await _seed_jobs(api_client)

    response = await api_client.get("/stats")

    assert response.status_code == 200
    assert response.json() == {
        "total_companies": 2,
        "total_jobs": 2,
        "active_jobs": 1,
        "closed_jobs": 1,
        "internship_jobs": 1,
        "new_grad_jobs": 1,
    }


@pytest.mark.asyncio
async def test_stats_return_zeroes_for_an_empty_database(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/stats")

    assert response.status_code == 200
    assert response.json() == {
        "total_companies": 0,
        "total_jobs": 0,
        "active_jobs": 0,
        "closed_jobs": 0,
        "internship_jobs": 0,
        "new_grad_jobs": 0,
    }
