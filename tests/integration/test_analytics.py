"""Private activity, aggregate authorization and accurate durable totals."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from app.api.deps import get_db
from app.api.v1.analytics import router
from app.db.models import AnalyticsEvent, EmailDelivery
from app.db.repository import DatabaseRepository
from app.notifications.api import subscriber
from app.notifications.security import Identity, identity
from app.schemas.job import NormalizedJob
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
async def analytics_client(
    application_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ANALYTICS_ADMIN_UIDS", "owner")
    app = FastAPI()
    app.include_router(router)

    async def db() -> AsyncIterator[AsyncSession]:
        async with application_sessions() as session, session.begin():
            yield session

    async def who(authorization: str = Header(default="")) -> Identity:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Sign in")
        uid = authorization.removeprefix("Bearer ")
        return Identity(uid, f"{uid}@example.com", uid != "unverified")

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[identity] = who
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        yield client


def headers(uid: str = "alice") -> dict:
    return {"Authorization": f"Bearer {uid}"}


def event(**changes: object) -> dict:
    return {"id": str(uuid4()), "kind": "page_view", "page": "/", **changes}


async def test_authentication_admin_allowlist_and_no_cross_user_query(
    analytics_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = analytics_client
    for path in ("/analytics/me", "/analytics/site", "/analytics/access"):
        assert (await client.get(path)).status_code == 401
    assert (await client.post("/analytics/events", json=event())).status_code == 401
    assert (await client.get("/analytics/site", headers=headers())).status_code == 403
    assert (await client.get("/analytics/access", headers=headers())).json() == {"admin": False}
    monkeypatch.setenv("ANALYTICS_ADMIN_UIDS", "owner,unverified")
    assert (await client.get("/analytics/site", headers=headers("unverified"))).status_code == 403
    assert (await client.get("/analytics/site", headers=headers("owner"))).status_code == 200


async def test_activity_is_idempotent_private_and_excludes_filter_secrets(
    analytics_client: httpx.AsyncClient, application_sessions: async_sessionmaker[AsyncSession]
) -> None:
    client = analytics_client
    body = event()
    for _ in range(2):
        assert (
            await client.post("/analytics/events", json=body, headers=headers())
        ).status_code == 200
    await client.post("/analytics/events", json=event(), headers=headers("bob"))
    filters = {"category": "New Grad", "remote_only": True, "search_used": True}
    await client.post(
        "/analytics/events", json=event(kind="filter_change", filters=filters), headers=headers()
    )
    me = (await client.get("/analytics/me?user_id=bob", headers=headers())).json()
    assert me["activity"]["page_views"] == 1
    assert me["activity"]["filter_changes"] == 1
    assert len(me["activity"]["trend"]) == 30
    assert me["activity"]["filters"]["category"] == [{"value": "New Grad", "count": 1}]
    assert (await client.get("/analytics/me", headers=headers("bob"))).json()["activity"][
        "filter_changes"
    ] == 0
    for bad in (
        event(user_id="bob"),
        event(kind="filter_change", filters={"query": "private search"}),
    ):
        assert (
            await client.post("/analytics/events", json=bad, headers=headers())
        ).status_code == 422
    async with application_sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AnalyticsEvent)) == 3
    site = (await client.get("/analytics/site", headers=headers("owner"))).json()
    assert site["active_users"] == {"1": 2, "7": 2, "30": 2}
    assert site["tracked_users"] == 2
    assert "alice" not in str(site) and "bob" not in str(site)


async def test_jobs_email_totals_preferences_and_activity_windows(
    analytics_client: httpx.AsyncClient,
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    now = datetime.now(UTC)
    async with application_sessions() as session, session.begin():
        await DatabaseRepository(session).save_job_posting(application_job)
        for uid in ("alice", "bob"):
            user = await subscriber(session, Identity(uid, f"{uid}@example.com", True))
            user.alerts = True
        for index, (uid, status, provider) in enumerate(
            [
                ("alice", "accepted", "provider-1"),
                ("alice", "delivered", "provider-2"),
                ("bob", "bounced", "provider-3"),
                ("alice", "pending", None),
                ("alice", "failed", None),
            ]
        ):
            session.add(
                EmailDelivery(
                    id=str(index),
                    subscriber_id=uid,
                    kind="alert",
                    dedupe_key=str(index),
                    job_ids=[],
                    status=status,
                    provider_id=provider,
                )
            )
        for days in (0, 3, 20, 40):
            session.add(
                AnalyticsEvent(
                    id=str(uuid4()),
                    user_id=f"user-{days}",
                    kind="heartbeat",
                    page="/",
                    created_at=now - timedelta(days=days),
                )
            )
    site = (await analytics_client.get("/analytics/site", headers=headers("owner"))).json()
    assert site["jobs_discovered"] == site["jobs_open"] == site["discovery_occurrences"] == 1
    assert site["emails"]["sent"] == 3  # Provider acceptance, not attempted requests.
    assert site["emails"]["delivered"] == 1
    assert site["email_subscribers"] == 2
    assert site["active_users"] == {"1": 1, "7": 2, "30": 3}
    me = (await analytics_client.get("/analytics/me", headers=headers())).json()
    assert me["emails"]["sent"] == 2
    assert me["emails"]["pending"] == 1 and me["emails"]["failed"] == 1
    assert me["preferences"]["alerts"] is True
    assert "sender" not in me["preferences"] and "key_ciphertext" not in str(me)


@pytest.mark.parametrize(
    "body",
    [
        event(kind="unknown"),
        event(page="/profile?token=secret"),
        event(kind="job_click"),
        event(filters={}),
        event(kind="heartbeat", job_id=1),
    ],
)
async def test_invalid_events_are_rejected(analytics_client: httpx.AsyncClient, body: dict) -> None:
    assert (
        await analytics_client.post("/analytics/events", json=body, headers=headers())
    ).status_code == 422


async def test_clicks_require_real_jobs_and_filters_require_known_companies(
    analytics_client: httpx.AsyncClient,
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session, session.begin():
        job = await DatabaseRepository(session).save_job_posting(application_job)
        job_id = job.id
    client = analytics_client
    assert (
        await client.post(
            "/analytics/events", headers=headers(), json=event(kind="job_click", job_id=999)
        )
    ).status_code == 404
    assert (
        await client.post(
            "/analytics/events", headers=headers(), json=event(kind="job_click", job_id=job_id)
        )
    ).status_code == 200
    assert (
        await client.post(
            "/analytics/events",
            headers=headers(),
            json=event(kind="filter_change", filters={"company": "not a company"}),
        )
    ).status_code == 422
    assert (
        await client.post(
            "/analytics/events",
            headers=headers(),
            json=event(kind="filter_change", filters={"company": "Fixture Co"}),
        )
    ).status_code == 200
    assert (await client.get("/analytics/me?days=91", headers=headers())).status_code == 422
    me = (await client.get("/analytics/me?days=7", headers=headers())).json()
    assert me["activity"]["job_clicks"] == 1
    assert me["activity"]["filters"]["company"] == [{"value": "Fixture Co", "count": 1}]
