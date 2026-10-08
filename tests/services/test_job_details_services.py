"""Validate JSON evidence filtering against the production PostgreSQL dialect."""

from decimal import Decimal

import pytest
from app.api.v1.jobs import list_jobs
from app.db.models import Base
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.test_job_details import (
    test_history_and_pay_filters_count_all_pages as _check_history_and_pay_filters,
)

pytestmark = pytest.mark.service_integration


async def test_postgres_evidence_history_and_salary_filters(postgres_engine: AsyncEngine) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    await _check_history_and_pay_filters(sessions)
    async with sessions() as db:
        result = await list_jobs(db, minimum_pay=Decimal(999999), pay_interval="year")
        assert result.total == 0
