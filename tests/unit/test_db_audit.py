"""Tests for database integrity auditing."""

from datetime import UTC, datetime, timedelta

import pytest
from app.db.models import Base, Company, JobPosting, JobType
from app.services.db_audit import DatabaseAuditService
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_healthy_database_has_no_findings() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        company = Company(name="Acme")
        session.add(company)
        await session.flush()
        job = _job(company.id, closed=False)
        session.add(job)
    async with sessions() as session:
        report = await DatabaseAuditService(session, stale_after=timedelta(days=30)).run()
    assert report.healthy
    await engine.dispose()


@pytest.mark.asyncio
async def test_reports_closed_without_log_and_stale_status() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        company = Company(name="Acme")
        session.add(company)
        await session.flush()
        job = _job(company.id, closed=True)
        job.updated_at = datetime.now(UTC) - timedelta(days=60)
        session.add(job)
    async with sessions() as session:
        report = await DatabaseAuditService(session, stale_after=timedelta(days=30)).run()
    assert {item.code for item in report.findings} == {
        "closed_without_status_log",
        "stale_closed_status",
    }
    await engine.dispose()


def _job(company_id: int, *, closed: bool) -> JobPosting:
    return JobPosting(
        company_id=company_id,
        title="Engineer",
        base_hash="a" * 64,
        content_hash="b" * 64,
        apply_url="https://example.test",
        location="Remote",
        season=2026,
        job_type=JobType.INTERNSHIP,
        is_closed=closed,
    )
