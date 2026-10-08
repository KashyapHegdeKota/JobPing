"""Read-only HTTP routes for discovering job postings."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import JSON, Numeric, Select, case, cast, func, literal, select, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import TableValuedAlias

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


def _array_rows(value: ColumnElement[Any], dialect: str, alias: str) -> TableValuedAlias:
    is_array = (
        func.json_typeof(value) if dialect == "postgresql" else func.json_type(value)
    ) == "array"
    empty = cast(literal("[]"), JSON) if dialect == "postgresql" else literal("[]")
    array = case((is_array, value), else_=empty)
    return (
        (func.json_array_elements(array) if dialect == "postgresql" else func.json_each(array))
        .table_valued("value")
        .alias(alias)
    )


def _discovery_filters(
    statement: Select[tuple[JobPosting]],
    *,
    dialect: str,
    policy: str | None,
    policy_value: str,
    h1b_history: bool,
    salary_reported: bool,
    minimum_pay: Decimal | None,
    pay_currency: str,
    pay_interval: str,
) -> Select[tuple[JobPosting]]:
    if policy:
        statement = statement.where(
            func.coalesce(JobPosting.details["policies"][policy]["value"].as_string(), "unknown")
            == policy_value
        )
    if h1b_history:
        rows = _array_rows(Company.immigration_records, dialect, "history_rows")
        value = type_coerce(rows.c.value, JSON)
        statement = statement.where(
            select(1)
            .select_from(rows)
            .where(value["kind"].as_string().in_(["h1b_filings", "h1b_approvals"]))
            .exists()
        )
    if salary_reported or minimum_pay is not None:
        rows = _array_rows(JobPosting.details["compensation"], dialect, "pay_rows")
        value = cast(rows.c.value, JSON) if dialect == "postgresql" else rows.c.value
        # Give SQLite's text value JSON operators without CAST(... AS JSON),
        # which would convert JSON text to a number under SQLite affinity rules.
        value = type_coerce(value, JSON)
        pay = select(1).select_from(rows)
        if minimum_pay is not None:
            pay = pay.where(
                value["currency"].as_string() == pay_currency,
                value["interval"].as_string() == pay_interval,
                cast(value["minimum"].as_string(), Numeric) >= minimum_pay,
            )
        statement = statement.where(pay.exists())
    return statement


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
    policy: Literal["cpt", "opt", "stem_opt", "sponsorship", "future_sponsorship"] | None = None,
    policy_value: Literal["allowed", "denied", "conditional", "conflicting", "unknown"] = "allowed",
    h1b_history: bool = False,
    salary_reported: bool = False,
    minimum_pay: Annotated[Decimal | None, Query(ge=0, le=100_000_000)] = None,
    pay_currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "USD",
    pay_interval: Literal["hour", "month", "year", "week"] = "year",
) -> PaginatedJobResponse:
    """Return a deterministic, filtered page of job postings."""
    filtered = _apply_filters(
        select(JobPosting).join(JobPosting.company),
        search=search,
        company=company,
        active=active,
    )
    filtered = _discovery_filters(
        filtered,
        dialect=db.bind.dialect.name,
        policy=policy,
        policy_value=policy_value,
        h1b_history=h1b_history,
        salary_reported=salary_reported,
        minimum_pay=minimum_pay,
        pay_currency=pay_currency,
        pay_interval=pay_interval,
    )
    count_statement = select(func.count()).select_from(filtered.order_by(None).subquery())
    total = int(await db.scalar(count_statement) or 0)

    jobs_statement = (
        filtered.options(joinedload(JobPosting.company))
        .order_by(
            func.coalesce(JobPosting.posted_at, JobPosting.created_at).desc(), JobPosting.id.desc()
        )
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
