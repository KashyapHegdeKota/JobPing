"""Shared scheduling and database serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationLock


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def next_cutoff(after: datetime, timezone: str) -> datetime:
    local = utc(after).astimezone(ZoneInfo(timezone))
    cutoff = datetime.combine(local.date(), time(20), local.tzinfo)
    if cutoff <= local:
        cutoff = datetime.combine(local.date() + timedelta(days=1), time(20), local.tzinfo)
    return cutoff.astimezone(UTC)


async def lock(session: AsyncSession) -> None:
    """Serialize admission, matching and claims across VM processes."""
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    await session.execute(insert(NotificationLock).values(id=1).on_conflict_do_nothing())
    await session.scalar(select(NotificationLock).where(NotificationLock.id == 1).with_for_update())
