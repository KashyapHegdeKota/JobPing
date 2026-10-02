"""Verify PostgreSQL JSON aggregation and committed activity isolation."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from app.api.deps import get_db
from app.api.v1.analytics import router
from app.db.models import Base
from app.notifications.security import Identity, identity
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.service_integration


async def test_postgres_analytics_records_and_aggregates_private_activity(
    postgres_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    current_uid = "owner"
    monkeypatch.setenv("ANALYTICS_ADMIN_UIDS", current_uid)

    async def who() -> Identity:
        return Identity(current_uid, "fixture@example.com", True)

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session, session.begin():
            yield session

    app.dependency_overrides[identity] = who
    app.dependency_overrides[get_db] = db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        event = {
            "id": str(uuid4()),
            "kind": "filter_change",
            "page": "/",
            "filters": {"category": "New Grad", "remote_only": True},
        }
        for _ in range(2):
            assert (await client.post("/analytics/events", json=event)).status_code == 200
        personal = (await client.get("/analytics/me")).json()
        assert personal["activity"]["filter_changes"] == 1
        assert personal["activity"]["filters"]["remote_only"] == [{"value": "true", "count": 1}]
        site = (await client.get("/analytics/site")).json()
        assert site["active_users"]["1"] == 1
        assert site["activity"]["trend"][-1]["active_users"] == 1
        current_uid = "other-user"
        assert (await client.get("/analytics/me")).json()["activity"]["filter_changes"] == 0
        assert (await client.get("/analytics/site")).status_code == 403
