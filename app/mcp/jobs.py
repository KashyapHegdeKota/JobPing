"""Job discovery tools exposed through the MCP boundary."""

from __future__ import annotations

from app.db.models import ApplicationStatus
from app.db.repository import DatabaseRepository


def _payload(
    job: object, status: ApplicationStatus = ApplicationStatus.QUEUED
) -> dict[str, object]:
    return {
        "job_id": job.id,  # type: ignore[attr-defined]
        "company": job.company.name,  # type: ignore[attr-defined]
        "title": job.title,  # type: ignore[attr-defined]
        "location": job.location,  # type: ignore[attr-defined]
        "apply_url": job.apply_url,  # type: ignore[attr-defined]
        "description": getattr(job, "description", None),
        "status": status.value,
    }


async def jobs_get_next(repository: DatabaseRepository) -> dict[str, object] | None:
    """Return the next eligible job, newest discovered first."""
    attempt = await repository.get_next_application()
    if attempt is None:
        return None
    return _payload(attempt.job, ApplicationStatus(attempt.status))


async def jobs_get(repository: DatabaseRepository, job_id: int) -> dict[str, object] | None:
    """Return one job and its current application state."""
    job = await repository.get_job_by_id(job_id)
    if job is None:
        return None
    attempt = await repository.get_application_attempt(job_id)
    status = ApplicationStatus.QUEUED if attempt is None else ApplicationStatus(attempt.status)
    return _payload(job, status)


__all__ = ["jobs_get", "jobs_get_next"]
