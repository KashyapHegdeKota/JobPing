"""Deterministic application queue built on the repository boundary."""

from __future__ import annotations

from app.applications.models import ApplicationJob
from app.db.repository import DatabaseRepository


class ApplicationQueue:
    """Select newest eligible open applications without fit scoring."""

    def __init__(self, repository: DatabaseRepository) -> None:
        self._repository = repository

    @staticmethod
    def _job(attempt: object) -> ApplicationJob:
        job = attempt.job  # type: ignore[attr-defined]
        description = getattr(job, "description", None)
        return ApplicationJob(
            job_id=job.id,
            company=job.company.name,
            title=job.title,
            location=job.location,
            apply_url=job.apply_url,
            description=description,
            status=attempt.status,  # type: ignore[attr-defined]
        )

    async def next(self) -> ApplicationJob | None:
        attempt = await self._repository.get_next_application()
        return None if attempt is None else self._job(attempt)

    async def list(self) -> list[ApplicationJob]:
        return [self._job(attempt) for attempt in await self._repository.list_application_queue()]


__all__ = ["ApplicationQueue"]
