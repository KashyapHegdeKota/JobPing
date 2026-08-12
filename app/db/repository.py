"""Async persistence operations for companies, job postings, and state history."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import event, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import Company, JobPosting, JobType, StatusLog
from app.events.publisher import EventPublisher, JobEventType
from app.schemas.job import NormalizedJob

logger = logging.getLogger(__name__)

_PENDING_EVENTS_KEY = "jobping_pending_events"


class DatabaseRepository:
    """Persist normalized jobs without committing caller-owned transactions.

    Each public method is atomic when called on an idle session. If the caller has
    already opened a transaction, the method joins it so several operations can be
    committed or rolled back as a single unit.
    """

    def __init__(self, session: AsyncSession, publisher: EventPublisher | None = None) -> None:
        self._session = session
        self._publisher = publisher
        self._publish_tasks: set[asyncio.Task[None]] = set()
        if publisher is not None and not session.sync_session.info.get("jobping_events_configured"):
            session.sync_session.info["jobping_events_configured"] = True
            event.listen(session.sync_session, "after_commit", self._after_commit)
            event.listen(session.sync_session, "after_rollback", self._after_rollback)

    def _after_commit(self, session: Session) -> None:
        info = session.info
        pending = info.pop(_PENDING_EVENTS_KEY, [])
        for event_data in pending:
            task = asyncio.get_running_loop().create_task(self._publish_event(event_data))
            self._publish_tasks.add(task)
            task.add_done_callback(self._publish_tasks.discard)
            task.add_done_callback(self._log_task_failure)

    @staticmethod
    def _after_rollback(session: Session) -> None:
        session.info.pop(_PENDING_EVENTS_KEY, None)

    @staticmethod
    def _log_task_failure(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Committed job event callback failed", exc_info=error)

    async def _publish_event(self, event_data: dict[str, object]) -> None:
        if self._publisher is None:
            return
        await self._publisher.publish_job_event(
            JobEventType(str(event_data["event_type"])),
            job_id=int(event_data["job_id"]),
            base_hash=str(event_data["base_hash"]),
            payload=cast(dict[str, Any], event_data["payload"]),
        )

    async def wait_for_pending_events(self) -> None:
        """Wait until post-commit publications scheduled by this repository finish."""
        while self._publish_tasks:
            await asyncio.gather(*tuple(self._publish_tasks))

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
            was_created = existing is None
            changed = False
            if was_created:
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
            if self._publisher is not None and (was_created or changed):
                event_type = JobEventType.JOB_CREATED if was_created else JobEventType.JOB_UPDATED
                self._session.sync_session.info.setdefault(_PENDING_EVENTS_KEY, []).append(
                    {
                        "event_type": event_type.value,
                        "job_id": existing.id,
                        "base_hash": existing.base_hash,
                        "payload": {
                            "company_id": existing.company_id,
                            "title": existing.title,
                            "apply_url": existing.apply_url,
                            "location": existing.location,
                            "season": existing.season,
                            "job_type": existing.job_type.value,
                            "is_closed": existing.is_closed,
                            "content_hash": existing.content_hash,
                        },
                    }
                )
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
