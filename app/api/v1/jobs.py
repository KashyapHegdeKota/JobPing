"""Read-only HTTP routes for discovering job postings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.deps import get_db
from app.db.models import Company, JobPosting
from app.schemas.api import JobResponse, PaginatedJobResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])

DatabaseSession = Annotated[AsyncSession, Depends(get_db)]
SearchQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=500,
        description="Case-insensitive text matched against job titles and company names.",
    ),
]
CompanyQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=255,
        description="Case-insensitive exact company name.",
    ),
]
ActiveQuery = Annotated[
    bool | None,
    Query(description="True for open jobs, false for closed jobs."),
]
PageQuery = Annotated[int, Query(ge=1)]
PageSizeQuery = Annotated[int, Query(ge=1, le=100)]


def _apply_filters(
    statement: Select[tuple[JobPosting]],
    *,
    search: str | None,
    company: str | None,
    active: bool | None,
) -> Select[tuple[JobPosting]]:
    """Apply discovery filters consistently to data and count queries."""
    if search is not None and (term := search.strip()):
        statement = statement.where(
            JobPosting.title.icontains(term, autoescape=True)
            | Company.name.icontains(term, autoescape=True)
        )
    if company is not None and (company_name := company.strip()):
        statement = statement.where(func.lower(Company.name) == company_name.lower())
    if active is not None:
        statement = statement.where(JobPosting.is_closed.is_(not active))
    return statement


@router.get("", response_model=PaginatedJobResponse)
async def list_jobs(
    db: DatabaseSession,
    search: SearchQuery = None,
    company: CompanyQuery = None,
    active: ActiveQuery = None,
    page: PageQuery = 1,
    page_size: PageSizeQuery = 20,
) -> PaginatedJobResponse:
    """Return a deterministic, filtered page of job postings."""
    filtered = _apply_filters(
        select(JobPosting).join(JobPosting.company),
        search=search,
        company=company,
        active=active,
    )
    count_statement = select(func.count()).select_from(filtered.order_by(None).subquery())
    total = int(await db.scalar(count_statement) or 0)

    jobs_statement = (
        filtered.options(joinedload(JobPosting.company))
        .order_by(JobPosting.created_at.desc(), JobPosting.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    jobs = (await db.scalars(jobs_statement)).all()

    return PaginatedJobResponse(
        items=[JobResponse.model_validate(job) for job in jobs],
        total=total,
        page=page,
        page_size=page_size,
    )


__all__ = ["router"]
