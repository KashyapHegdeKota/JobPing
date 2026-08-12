"""Async persistence operations for companies, job postings, and state history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, JobPosting, JobType, StatusLog
from app.schemas.job import NormalizedJob


class DatabaseRepository:
    """Persist normalized jobs without committing caller-owned transactions.

    Each public method is atomic when called on an idle session. If the caller has
    already opened a transaction, the method joins it so several operations can be
    committed or rolled back as a single unit.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        if self._session.in_transaction():
            yield
        else:
            async with self._session.begin():
                yield

    async def upsert_company(self, name: str, domain: str | None = None) -> Company:
        """Return the company identified by name, creating it when absent."""
        normalized_name = name.strip()
        normalized_domain = domain.strip().lower() if domain else None
        if not normalized_name:
            raise ValueError("company name must not be empty")

        async with self._transaction():
            dialect = self._session.bind.dialect.name if self._session.bind else ""
            if dialect in {"postgresql", "sqlite"}:
                insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
                statement = insert(Company).values(name=normalized_name, domain=normalized_domain)
                statement = statement.on_conflict_do_update(
                    index_elements=[Company.name],
                    set_={"domain": func.coalesce(statement.excluded.domain, Company.domain)},
                ).returning(Company)
                company = (await self._session.scalars(statement)).one()
            else:
                company = await self._session.scalar(
                    select(Company).where(Company.name == normalized_name)
                )
                if company is None:
                    company = Company(name=normalized_name, domain=normalized_domain)
                    self._session.add(company)
                else:
                    if normalized_domain is not None:
                        company.domain = normalized_domain
                await self._session.flush()
            return company

    async def save_job_posting(self, normalized_job: NormalizedJob) -> JobPosting:
        """Insert or update a posting using its stable base hash identity."""
        async with self._transaction():
            company_id = normalized_job.company_id
            if company_id is None:
                if normalized_job.company_name is None:
                    raise ValueError("normalized job requires company_id or company_name")
                company_id = (await self.upsert_company(normalized_job.company_name)).id

            existing = await self._session.scalar(
                select(JobPosting).where(JobPosting.base_hash == normalized_job.base_hash)
            )
            values = {
                "company_id": company_id,
                "title": normalized_job.title,
                "content_hash": normalized_job.content_hash,
                "apply_url": str(normalized_job.apply_url),
                "location": normalized_job.location,
                "season": normalized_job.season,
                "job_type": JobType(normalized_job.job_type),
                "is_closed": normalized_job.is_closed,
            }
            if existing is None:
                existing = JobPosting(base_hash=normalized_job.base_hash, **values)
                if normalized_job.created_at is not None:
                    existing.created_at = normalized_job.created_at
                if normalized_job.updated_at is not None:
                    existing.updated_at = normalized_job.updated_at
                self._session.add(existing)
            else:
                changed = any(getattr(existing, key) != value for key, value in values.items())
                for key, value in values.items():
                    setattr(existing, key, value)
                if changed:
                    existing.updated_at = normalized_job.updated_at or datetime.now(UTC)
            await self._session.flush()
            return existing

    async def log_status_change(
        self, job_id: int, previous_state: str | None, new_state: str
    ) -> StatusLog:
        """Append a genuine state transition to a posting's immutable history."""
        old = previous_state.strip() if previous_state is not None else None
        new = new_state.strip()
        if not new:
            raise ValueError("new state must not be empty")
        if old == new:
            raise ValueError("status log requires an actual state transition")

        async with self._transaction():
            if await self._session.get(JobPosting, job_id) is None:
                raise ValueError(f"job posting {job_id} does not exist")
            status_log = StatusLog(job_id=job_id, previous_state=old, new_state=new)
            self._session.add(status_log)
            await self._session.flush()
            return status_log


__all__ = ["DatabaseRepository"]
