"""Authenticated personal activity and admin-only aggregate site analytics."""

from datetime import UTC, datetime, timedelta
from os import environ
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import get_db
from app.db.models import (
    AnalyticsEvent,
    Base,
    Company,
    EmailDelivery,
    JobMatch,
    JobOccurrence,
    JobPosting,
    Subscriber,
)
from app.notifications.security import Identity, identity

router = APIRouter(prefix="/analytics", tags=["analytics"])
DB = Annotated[AsyncSession, Depends(get_db)]
Who = Annotated[Identity, Depends(identity)]
Days = Annotated[int, Query(ge=1, le=90)]


class FeedFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["All", "Summer 2027", "New Grad"] = "All"
    remote_only: bool = False
    date_filter: Literal["All Time", "Past 24 hours", "Past Week", "Past Month"] = "All Time"
    company: str | None = Field(default=None, min_length=1, max_length=255)
    search_used: bool = False


class ActivityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    kind: Literal["page_view", "heartbeat", "filter_change", "job_click"]
    page: Literal["/", "/profile", "/profile/recaps", "/trackers", "/referrals", "/activity"]
    filters: FeedFilters | None = None
    job_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def consistent_payload(self) -> "ActivityInput":
        if (self.kind == "filter_change") != (self.filters is not None):
            raise ValueError("Filters are required only for filter-change events")
        if (self.kind == "job_click") != (self.job_id is not None):
            raise ValueError("A job ID is required only for job-click events")
        if self.kind in {"filter_change", "job_click"} and self.page != "/":
            raise ValueError("Feed interactions must belong to the home page")
        return self


def is_admin(who: Identity) -> bool:
    allowed = {value.strip() for value in environ.get("ANALYTICS_ADMIN_UIDS", "").split(",")}
    return who.verified and who.uid in allowed


async def count(session: AsyncSession, model: type[Base], *where: ColumnElement[bool]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


@router.get("/access")
async def access(who: Who) -> dict:
    return {"admin": is_admin(who)}


@router.post("/events")
async def record_activity(body: ActivityInput, session: DB, who: Who) -> dict:
    if body.job_id is not None and await session.get(JobPosting, body.job_id) is None:
        raise HTTPException(404, "Job not found")
    if body.filters and body.filters.company is not None:
        if not await session.scalar(select(Company.id).where(Company.name == body.filters.company)):
            raise HTTPException(422, "Choose a company from the feed")
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    statement = (
        insert(AnalyticsEvent)
        .values(
            id=str(body.id),
            user_id=who.uid,
            kind=body.kind,
            page=body.page,
            filters=body.filters.model_dump() if body.filters else None,
            job_id=body.job_id,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(AnalyticsEvent.id)
    )
    await session.execute(statement)
    return {"ok": True}


async def activity(session: AsyncSession, days: int, uid: str | None = None) -> dict:
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    scope = [AnalyticsEvent.user_id == uid] if uid is not None else []
    window = [*scope, AnalyticsEvent.created_at >= start]
    kinds = dict(
        (
            await session.execute(
                select(AnalyticsEvent.kind, func.count())
                .where(*window)
                .group_by(AnalyticsEvent.kind)
            )
        ).all()
    )
    daily = (
        await session.execute(
            select(
                func.date(AnalyticsEvent.created_at),
                func.count(func.distinct(AnalyticsEvent.user_id)),
                func.count().filter(AnalyticsEvent.kind == "page_view"),
            )
            .where(*window)
            .group_by(func.date(AnalyticsEvent.created_at))
        )
    ).all()
    dates = {str(day): (users, views) for day, users, views in daily}
    trend = []
    for offset in range(days):
        day = (start + timedelta(days=offset)).date().isoformat()
        users, views = dates.get(day, (0, 0))
        trend.append({"date": day, "active_users": users, "page_views": views})
    breakdown = {}
    for field in ("category", "remote_only", "date_filter", "company", "search_used"):
        value = AnalyticsEvent.filters[field].as_string()
        rows = (
            await session.execute(
                select(value, func.count())
                .where(*window, AnalyticsEvent.kind == "filter_change")
                .group_by(value)
                .order_by(func.count().desc(), value)
            )
        ).all()
        breakdown[field] = [
            {"value": str(v) if v is not None else "All", "count": n} for v, n in rows
        ]
    return {
        "days": days,
        "timezone": "UTC",
        "trend": trend,
        "filters": breakdown,
        "page_views": kinds.get("page_view", 0),
        "filter_changes": kinds.get("filter_change", 0),
        "job_clicks": kinds.get("job_click", 0),
        "tracking_started_at": await session.scalar(
            select(func.min(AnalyticsEvent.created_at)).where(*scope)
        ),
    }


async def email_counts(session: AsyncSession, uid: str | None = None) -> dict:
    scope = [EmailDelivery.subscriber_id == uid] if uid is not None else []
    statuses = dict(
        (
            await session.execute(
                select(EmailDelivery.status, func.count())
                .where(*scope)
                .group_by(EmailDelivery.status)
            )
        ).all()
    )
    return {
        "sent": await count(session, EmailDelivery, *scope, EmailDelivery.provider_id.is_not(None)),
        "delivered": statuses.get("delivered", 0),
        "pending": statuses.get("pending", 0),
        "failed": statuses.get("failed", 0),
        "bounced": statuses.get("bounced", 0),
        "complained": statuses.get("complained", 0),
        "reconcile": statuses.get("reconcile", 0),
    }


@router.get("/me")
async def personal(session: DB, who: Who, days: Days = 30) -> dict:
    user = await session.get(Subscriber, who.uid)
    return {
        "activity": await activity(session, days, who.uid),
        "emails": await email_counts(session, who.uid),
        "matched_occurrences": await count(session, JobMatch, JobMatch.subscriber_id == who.uid),
        "preferences": (
            {
                "alerts": user.alerts,
                "recap": user.recap,
                "job_types": user.job_types,
                "seasons": user.seasons,
                "timezone": user.timezone,
            }
            if user
            else None
        ),
    }


@router.get("/site")
async def site(session: DB, who: Who, days: Days = 30) -> dict:
    if not is_admin(who):
        raise HTTPException(403, "Site analytics requires an admin account")
    active = {}
    for period in (1, 7, 30):
        active[str(period)] = int(
            await session.scalar(
                select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                    AnalyticsEvent.created_at >= datetime.now(UTC) - timedelta(days=period)
                )
            )
            or 0
        )
    return {
        "active_users": active,
        "tracked_users": int(
            await session.scalar(select(func.count(func.distinct(AnalyticsEvent.user_id)))) or 0
        ),
        "jobs_discovered": await count(session, JobPosting),
        "jobs_open": await count(session, JobPosting, JobPosting.is_closed.is_(False)),
        "discovery_occurrences": await count(
            session, JobOccurrence, JobOccurrence.kind == "discovered"
        ),
        "reposted_occurrences": await count(
            session, JobOccurrence, JobOccurrence.kind == "reposted"
        ),
        "email_subscribers": await count(
            session,
            Subscriber,
            Subscriber.verified.is_(True),
            Subscriber.suppressed.is_(False),
            Subscriber.alerts.is_(True) | Subscriber.recap.is_(True),
        ),
        "emails": await email_counts(session),
        "activity": await activity(session, days),
    }
