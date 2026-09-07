"""Network-free notification regressions with an isolated durable database."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.api.deps import get_db
from app.db.models import Base, DiscoveryEvent, EmailDelivery, JobMatch, JobPosting, Subscriber
from app.db.repository import DatabaseRepository
from app.main import create_app
from app.notifications.api import Settings, put_settings, subscriber
from app.notifications.core import next_cutoff, utc
from app.notifications.render import payload, safe_url
from app.notifications.security import Identity, decrypt, encrypt, identity
from app.notifications.worker import NotificationWorker, budget, delivery, make_recaps, match_events
from app.schemas.job import NormalizedJob
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from svix.webhooks import Webhook

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notifications.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFICATION_APP_URL", "https://jobping.example")
    monkeypatch.setenv("NOTIFICATION_API_URL", "https://api.jobping.example")
    monkeypatch.setenv("RESEND_FROM", "no-reply@jobping.example")
    monkeypatch.setenv("RESEND_API_KEY", "shared-test-key")
    monkeypatch.setenv(
        "NOTIFICATION_ENCRYPTION_KEYS", json.dumps({"v1": Fernet.generate_key().decode()})
    )
    monkeypatch.setenv("NOTIFICATIONS_SEND_ENABLED", "false")


def job(index: int = 1, **changes: object) -> NormalizedJob:
    return NormalizedJob.model_validate(
        {
            "company_name": "Acme",
            "title": f"Engineer {index}",
            "base_hash": f"{index:064x}",
            "content_hash": f"{index:064x}",
            "apply_url": "https://example.com/apply",
            "location": "Phoenix",
            "season": 2027,
            "job_type": "internship",
            "is_closed": False,
            **changes,
        }
    )


async def seed(session: AsyncSession, uid: str = "alice", **changes: object) -> Subscriber:
    user = await subscriber(session, Identity(uid, f"{uid}@example.com", True))
    user.alerts, user.recap = True, True
    user.opted_at, user.window_start = NOW - timedelta(days=1), NOW - timedelta(hours=16)
    user.next_recap = NOW + timedelta(hours=8)
    for name, value in changes.items():
        setattr(user, name, value)
    await session.flush()
    return user


async def discover(session: AsyncSession, index: int = 1, **changes: object) -> JobPosting:
    posting = await DatabaseRepository(session).save_job_posting(job(index, **changes))
    event = await session.get(DiscoveryEvent, posting.id)
    if event:
        event.discovered_at = NOW
    await session.flush()
    return posting


@pytest.mark.parametrize("bulk", [False, True])
async def test_discovery_is_transactional_and_unique(
    sessions: async_sessionmaker, bulk: bool
) -> None:
    async with sessions() as session:
        async with session.begin():
            repo = DatabaseRepository(session)
            if bulk:
                await repo.bulk_upsert_job_postings([job()])
            else:
                await repo.save_job_posting(job())
        async with session.begin():
            await repo.bulk_upsert_job_postings([job(title="Changed"), job(2, is_closed=True)])
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1
        await session.rollback()
        async with session.begin():
            await repo.save_job_posting(job(3))
            await session.rollback()
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1


async def test_bootstrap_has_no_events(
    sessions: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with sessions() as session, session.begin():
        await DatabaseRepository(session, suppress_notifications=True).bulk_upsert_job_postings(
            [job()]
        )
        monkeypatch.setenv("NOTIFICATIONS_SUPPRESS_DISCOVERY", "true")
        await DatabaseRepository(session).save_job_posting(job(2))
        assert await session.scalar(select(func.count()).select_from(DiscoveryEvent)) == 0


async def test_matching_preferences_no_duplicates_or_backfill(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        await seed(session)
        await seed(session, "bob", seasons=[2026])
        await seed(session, "late", opted_at=NOW + timedelta(seconds=1))
        await discover(session)
        await match_events(session, NOW)
        await match_events(session, NOW)
        assert list(await session.scalars(select(JobMatch.subscriber_id))) == ["alice"]
        assert await session.scalar(select(func.count()).select_from(EmailDelivery)) == 1


async def test_recap_repeats_sent_jobs_and_folds_unsent_once(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        await discover(session)
        await discover(session, 2)
        await match_events(session, NOW)
        alerts = list(
            await session.scalars(select(EmailDelivery).order_by(EmailDelivery.dedupe_key))
        )
        alerts[0].status, alerts[0].first_attempt = "accepted", NOW
        await make_recaps(session, NOW + timedelta(hours=9))
        await make_recaps(session, NOW + timedelta(hours=9))
        recaps = list(
            await session.scalars(select(EmailDelivery).where(EmailDelivery.kind == "recap"))
        )
        assert len(recaps) == 1 and len(recaps[0].job_ids) == 2
        assert alerts[1].status == "recap_only"
        assert utc(user.window_start) == NOW + timedelta(hours=8)


async def test_empty_and_downtime_recaps(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        await make_recaps(session, NOW + timedelta(hours=9))
        assert await session.scalar(select(func.count()).select_from(EmailDelivery)) == 0
        user.window_start, user.next_recap = NOW - timedelta(hours=16), NOW + timedelta(hours=8)
        await discover(session)
        await match_events(session, NOW)
        await make_recaps(session, NOW + timedelta(days=3))
        assert (
            await session.scalar(
                select(func.count()).select_from(EmailDelivery).where(EmailDelivery.kind == "recap")
            )
            == 1
        )
        assert utc(user.next_recap) > NOW + timedelta(days=3)


@pytest.mark.parametrize(
    "start,expected",
    [
        ("2026-03-07T20:00:00-05:00", "2026-03-09T00:00:00+00:00"),
        ("2026-10-31T20:00:00-04:00", "2026-11-02T01:00:00+00:00"),
    ],
)
def test_cutoff_handles_dst(start: str, expected: str) -> None:
    assert next_cutoff(datetime.fromisoformat(start), "America/New_York").isoformat() == expected


async def test_new_timezone_preserves_window(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        start = user.window_start
        await put_settings(
            Settings(alerts=True, recap=True, timezone="America/Phoenix"),
            session,
            Identity("alice", "alice@example.com", True),
        )
        assert user.window_start == start
        assert utc(user.next_recap).astimezone(ZoneInfo("America/Phoenix")).hour == 20


async def test_budget_reserves_recaps_and_isolates_accounts(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        for i in range(89):
            item = delivery(user, "alert", f"charged:{i}", [], NOW)
            item.account, item.first_attempt, item.status = "shared", NOW, "accepted"
            session.add(item)
        await session.flush()
        assert not await budget(session, "shared", "alert", NOW)
        assert await budget(session, "shared", "recap", NOW)
        assert await budget(session, "alice", "alert", NOW)


async def queued(sessions: async_sessionmaker, **changes: object) -> str:
    async with sessions() as session, session.begin():
        await seed(session, **changes)
        await discover(session)
        await match_events(session, NOW)
        return await session.scalar(select(EmailDelivery.id))


async def test_send_retries_frozen_payload_after_restart(sessions: async_sessionmaker) -> None:
    item_id = await queued(sessions)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("uncertain", request=request)
        return httpx.Response(200, json={"id": "resend-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = NotificationWorker(sessions, client, verify_recipient=AsyncMock(return_value=True))
        assert await worker.send_one(NOW)
        async with sessions() as session, session.begin():
            posting = await session.get(JobPosting, 1)
            posting.title = "Changed after request"
        restarted = NotificationWorker(
            sessions, client, verify_recipient=AsyncMock(return_value=True)
        )
        assert await restarted.send_one(NOW + timedelta(minutes=2))
    assert requests[0].content == requests[1].content
    assert requests[0].headers["idempotency-key"] == requests[1].headers["idempotency-key"]
    async with sessions() as session:
        item = await session.get(EmailDelivery, item_id)
        assert item.status == "accepted" and item.provider_id == "resend-1"


async def test_lease_prevents_double_claim_and_expired_uncertain_send(
    sessions: async_sessionmaker,
) -> None:
    item_id = await queued(sessions)
    async with httpx.AsyncClient() as client:
        worker = NotificationWorker(sessions, client, verify_recipient=AsyncMock(return_value=True))
        results = await asyncio.gather(worker.claim(NOW), worker.claim(NOW))
        assert sum(result is not None for result in results) == 1
        assert await worker.claim(NOW + timedelta(days=1)) is None
    async with sessions() as session:
        assert (await session.get(EmailDelivery, item_id)).status == "reconcile"


async def test_byok_auth_failure_never_falls_back(sessions: async_sessionmaker) -> None:
    await queued(
        sessions,
        provider="byok",
        key_ciphertext=encrypt("private-test-key"),
        sender="alerts@example.com",
        status="ready",
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"name": "invalid_api_key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = NotificationWorker(sessions, client, verify_recipient=AsyncMock(return_value=True))
        await worker.send_one(NOW)
        assert not await worker.send_one(NOW + timedelta(minutes=10))
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer private-test-key"
    async with sessions() as session:
        assert (await session.get(Subscriber, "alice")).status == "sender_error"


async def test_disabled_changed_or_deleted_recipient_cancels(sessions: async_sessionmaker) -> None:
    item_id = await queued(sessions)
    async with httpx.AsyncClient() as client:
        worker = NotificationWorker(
            sessions, client, verify_recipient=AsyncMock(return_value=False)
        )
        assert await worker.claim(NOW) is None
    async with sessions() as session:
        assert (await session.get(EmailDelivery, item_id)).status == "cancelled"


async def test_html_escapes_scraped_content_and_closed_jobs(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        posting = await discover(session, title='<script>alert("oops")</script>')
        item = delivery(user, "alert", "render", [posting.id], NOW)
        body = await payload(session, item, user)
        assert "<script>" not in body["html"] and "&lt;script&gt;" in body["html"]
        assert "<script>" in body["text"]
        posting.is_closed = True
        body = await payload(session, item, user)
        assert "Applications closed" in body["html"] and ">Apply now<" not in body["html"]
        assert safe_url("javascript:alert(1)") == ""


def test_key_rotation_and_tamper(monkeypatch: pytest.MonkeyPatch) -> None:
    first = Fernet.generate_key().decode()
    second = Fernet.generate_key().decode()
    monkeypatch.setenv("NOTIFICATION_ENCRYPTION_KEYS", json.dumps({"v1": first, "v2": second}))
    encrypted = encrypt("never-return-me")
    monkeypatch.setenv("NOTIFICATION_KEY_VERSION", "v2")
    assert decrypt(encrypted) == "never-return-me"
    assert encrypt("new-key").startswith("v2:")
    with pytest.raises(ValueError):
        decrypt(encrypted[:-4] + "nope")


@pytest_asyncio.fixture
async def api(sessions: async_sessionmaker) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(startup_health_checks=False)

    async def db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session, session.begin():
            yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[identity] = lambda: Identity("alice", "alice@example.com", True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://api.example"
    ) as client:
        client.test_app = app
        yield client


async def test_provider_key_is_write_only_and_validation_redacted(
    api: httpx.AsyncClient, sessions: async_sessionmaker
) -> None:
    response = await api.put(
        "/api/v1/me/email-provider",
        json={"sender": "alerts@example.com", "api_key": "super-secret-key"},
    )
    assert response.status_code == 200, response.text
    assert "super-secret" not in response.text and "ciphertext" not in response.text
    bad = await api.put(
        "/api/v1/me/email-provider", json={"sender": "invalid", "api_key": "super-secret-key"}
    )
    assert bad.status_code == 422 and "super-secret" not in bad.text
    async with sessions() as session:
        user = await session.get(Subscriber, "alice")
        assert user.key_ciphertext != "super-secret-key"
        assert decrypt(user.key_ciphertext) == "super-secret-key"


async def test_unauthenticated_and_unverified_opt_in_rejected(api: httpx.AsyncClient) -> None:
    api.test_app.dependency_overrides[identity] = lambda: Identity(
        "alice", "alice@example.com", False
    )
    response = await api.put("/api/v1/me/notification-settings", json={"alerts": True})
    assert response.status_code == 403
    api.test_app.dependency_overrides.pop(identity)
    assert (await api.get("/api/v1/me/notification-settings")).status_code == 401


async def test_capacity_and_byok_can_bypass_shared_cap(
    api: httpx.AsyncClient, sessions: async_sessionmaker
) -> None:
    async with sessions() as session, session.begin():
        for i in range(10):
            await seed(session, f"user{i}")
    assert (
        await api.put("/api/v1/me/notification-settings", json={"recap": True})
    ).status_code == 409
    await api.put(
        "/api/v1/me/email-provider",
        json={"sender": "alerts@example.com", "api_key": "test-private-key"},
    )
    assert (
        await api.put("/api/v1/me/notification-settings", json={"recap": True})
    ).status_code == 200


async def test_unsubscribe_get_is_safe_post_cancels_and_recap_is_private(
    api: httpx.AsyncClient, sessions: async_sessionmaker
) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        item = delivery(user, "recap", "private", [], NOW)
        item.window_start, item.window_end = NOW - timedelta(days=1), NOW
        session.add(item)
        token, recap_id = user.unsubscribe_token, item.id
    endpoint = f"/api/v1/notifications/unsubscribe/{token}"
    assert (await api.get(endpoint)).status_code == 200
    async with sessions() as session:
        assert (await session.get(Subscriber, "alice")).alerts
    assert (await api.post(endpoint)).status_code == 200
    assert (await api.post(endpoint)).status_code == 200
    async with sessions() as session:
        assert not (await session.get(Subscriber, "alice")).alerts
        assert (await session.get(EmailDelivery, recap_id)).status == "cancelled"
    api.test_app.dependency_overrides[identity] = lambda: Identity("bob", "bob@example.com", True)
    assert (await api.get(f"/api/v1/me/recaps/{recap_id}")).status_code == 404


async def test_invalid_webhook_does_not_mutate(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(b"secret-key").decode())
    response = await api.post(
        "/api/v1/notifications/webhooks/shared", json={"type": "email.bounced"}
    )
    assert response.status_code == 400


async def test_signed_webhooks_deduplicate_and_suppress(
    api: httpx.AsyncClient, sessions: async_sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "whsec_" + base64.b64encode(b"notification-signing-secret").decode()
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
    async with sessions() as session, session.begin():
        user = await seed(session)
        item = delivery(user, "alert", "bounced-email", [], NOW)
        item.account, item.provider_id, item.status = "shared", "email-1", "accepted"
        item.payload = {"to": [user.email]}
        session.add(item)
        session.add(delivery(user, "alert", "pending-email", [], NOW))
    raw = json.dumps({"type": "email.bounced", "data": {"email_id": "email-1"}})
    timestamp = datetime.now(UTC)
    headers = {
        "svix-id": "event-1",
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": Webhook(secret).sign("event-1", timestamp, raw),
    }
    endpoint = "/api/v1/notifications/webhooks/shared"
    assert (await api.post(endpoint, content=raw, headers=headers)).status_code == 200
    assert (await api.post(endpoint, content=raw, headers=headers)).json()["status"] == "duplicate"
    async with sessions() as session:
        assert (await session.get(Subscriber, "alice")).suppressed
        assert (
            await session.scalar(
                select(func.count())
                .select_from(EmailDelivery)
                .where(EmailDelivery.status == "pending")
            )
            == 0
        )
    assert (
        await api.put("/api/v1/me/notification-settings", json={"alerts": True})
    ).status_code == 409


async def test_provider_quota_blocks_only_that_account(sessions: async_sessionmaker) -> None:
    await queued(sessions)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, json={"name": "daily_quota_exceeded"})
        )
    ) as client:
        worker = NotificationWorker(sessions, client, verify_recipient=AsyncMock(return_value=True))
        await worker.send_one(NOW)
    async with sessions() as session:
        assert not await budget(session, "shared", "recap", NOW)
        assert await budget(session, "another-account", "recap", NOW)


async def test_oversized_recap_contains_complete_link(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        postings = await DatabaseRepository(
            session, suppress_notifications=True
        ).bulk_upsert_job_postings(
            [job(i, title="Long title " * 40 + str(i)) for i in range(1, 121)]
        )
        item = delivery(user, "recap", "large-recap", [p.id for p in postings], NOW)
        item.window_start, item.window_end = NOW - timedelta(days=1), NOW
        result = await payload(session, item, user)
        assert len(result["html"].encode()) < 90_000
        assert "120 new matches" in result["subject"]
        assert "more matches" in result["html"]
        assert f"/profile/recaps/{item.id}" in result["text"]


async def test_unsent_recaps_combine_after_quota_pause(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        await seed(session)
        await discover(session)
        await match_events(session, NOW)
        await make_recaps(session, NOW + timedelta(hours=9))
        posting = await discover(session, 2)
        (await session.get(DiscoveryEvent, posting.id)).discovered_at = NOW + timedelta(days=1)
        await match_events(session, NOW + timedelta(days=1))
        await make_recaps(session, NOW + timedelta(days=1, hours=9))
        recaps = list(
            await session.scalars(select(EmailDelivery).where(EmailDelivery.kind == "recap"))
        )
        assert len(recaps) == 1 and len(recaps[0].job_ids) == 2


async def test_preferences_change_only_future_discoveries(sessions: async_sessionmaker) -> None:
    async with sessions() as session, session.begin():
        await seed(session)
        await discover(session)
        await put_settings(
            Settings(alerts=True, recap=True, seasons=[2026]),
            session,
            Identity("alice", "alice@example.com", True),
        )
        await discover(session, 2)
        await match_events(session, NOW)
        assert list(await session.scalars(select(JobMatch.job_id))) == [1]


async def test_provider_removal_disables_optins(api: httpx.AsyncClient) -> None:
    await api.put(
        "/api/v1/me/email-provider",
        json={"sender": "alerts@example.com", "api_key": "private-test-key"},
    )
    await api.put("/api/v1/me/notification-settings", json={"alerts": True, "recap": True})
    response = await api.delete("/api/v1/me/email-provider")
    assert response.status_code == 200
    data = response.json()
    assert not data["alerts"] and not data["recap"] and not data["key_configured"]


async def test_dry_run_never_calls_resend(sessions: async_sessionmaker) -> None:
    await queued(sessions)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected send"))
    ) as client:
        worker = NotificationWorker(sessions, client)
        await worker.tick()


def test_notification_migration_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'migration.db'}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0003_checkpoint_stage")
    command.upgrade(config, "head")
    command.check(config)


async def test_timezone_detection_does_not_overwrite_saved_choice(api: httpx.AsyncClient) -> None:
    first = await api.get("/api/v1/me/notification-settings?timezone=America/Phoenix")
    assert first.json()["timezone"] == "America/Phoenix"
    await api.put("/api/v1/me/notification-settings", json={"timezone": "UTC"})
    again = await api.get("/api/v1/me/notification-settings?timezone=America/Phoenix")
    assert again.json()["timezone"] == "UTC"
