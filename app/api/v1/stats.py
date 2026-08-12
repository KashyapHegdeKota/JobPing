"""System-wide job discovery analytics."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.models import Company, JobPosting, JobType
from app.schemas.api import SystemStatsResponse

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("", response_model=SystemStatsResponse)
async def get_system_stats(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> SystemStatsResponse:
    """Return aggregate company, lifecycle, and job-type counts."""
    total_companies = await session.scalar(select(func.count()).select_from(Company))
    job_counts = (
        await session.execute(
            select(
                func.count(JobPosting.id),
                func.coalesce(func.sum(case((JobPosting.is_closed.is_(False), 1), else_=0)), 0),
                func.coalesce(func.sum(case((JobPosting.is_closed.is_(True), 1), else_=0)), 0),
                func.coalesce(
                    func.sum(case((JobPosting.job_type == JobType.INTERNSHIP, 1), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((JobPosting.job_type == JobType.NEW_GRAD, 1), else_=0)), 0
                ),
            )
        )
    ).one()

    total_jobs, active_jobs, closed_jobs, internship_jobs, new_grad_jobs = job_counts
    return SystemStatsResponse(
        total_companies=int(total_companies or 0),
        total_jobs=int(total_jobs),
        active_jobs=int(active_jobs),
        closed_jobs=int(closed_jobs),
        internship_jobs=int(internship_jobs),
        new_grad_jobs=int(new_grad_jobs),
    )


__all__ = ["router"]
