"""Network-free notification regressions with an isolated durable database."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
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
from app.db.models import (
    Base,
    DiscoveryEvent,
    EmailDelivery,
    JobMatch,
    JobOccurrence,
    JobPosting,
    Subscriber,
)
from app.db.repository import DatabaseRepository
from app.main import create_app
from app.notifications.api import Settings, put_settings, subscriber
from app.notifications.core import next_cutoff, utc
from app.notifications.render import occurrence_rows, payload, safe_url
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
    posting = await DatabaseRepository(session).save_job_posting(
        job(index, observed_at=NOW, **changes)
    )
    event = await session.scalar(
        select(DiscoveryEvent)
        .where(DiscoveryEvent.job_id == posting.id)
        .order_by(DiscoveryEvent.id)
        .limit(1)
    )
    if event:
        event.discovered_at = NOW
    await session.flush()
    return posting


async def repost(session: AsyncSession, index: int = 1) -> JobPosting:
    repository = DatabaseRepository(session)
    previous_id = f"{index}-old"
    await repository.save_job_posting(
        job(
            index,
            source="greenhouse",
            source_id=f"greenhouse:acme:{previous_id}",
            identity_namespace="greenhouse:acme",
            external_job_id=previous_id,
            observed_at=NOW,
        )
    )
    await repository.save_job_posting(
        job(
            index,
            is_closed=True,
            observed_at=NOW,
            source="greenhouse",
            source_id=f"greenhouse:acme:{previous_id}",
            identity_namespace="greenhouse:acme",
            external_job_id=previous_id,
        )
    )
    posting = await repository.save_job_posting(
        job(
            index,
            is_closed=False,
            observed_at=NOW + timedelta(minutes=1),
            posted_at=NOW + timedelta(minutes=1),
            source="greenhouse",
            source_id=f"greenhouse:acme:{index}-new",
            identity_namespace="greenhouse:acme",
            external_job_id=f"{index}-new",
            occurrence_kind="reposted",
        )
    )
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
        repo = DatabaseRepository(session, suppress_notifications=True)
        posting = await repo.save_job_posting(job())
        await repo.save_job_posting(job(is_closed=True, observed_at=NOW + timedelta(minutes=1)))
        await repo.save_job_posting(
            job(
                observed_at=NOW + timedelta(minutes=2),
                posted_at=NOW + timedelta(minutes=2),
                source="greenhouse",
                source_id="greenhouse:acme:1-new",
                identity_namespace="greenhouse:acme",
                external_job_id="1-new",
                occurrence_kind="reposted",
            )
        )
        monkeypatch.setenv("NOTIFICATIONS_SUPPRESS_DISCOVERY", "true")
        await DatabaseRepository(session).save_job_posting(job(2))
        assert posting.id == 1
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


async def test_subscriber_matches_each_occurrence_once_and_repost_alert_is_labeled(
    sessions: async_sessionmaker,
) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session, recap=False)
        await discover(session)
        await match_events(session, NOW)
        await repost(session)
        repost_event = await session.scalar(
            select(DiscoveryEvent)
            .where(DiscoveryEvent.event_type == "reposted")
            .order_by(DiscoveryEvent.id.desc())
        )
        repost_event.discovered_at = NOW + timedelta(minutes=1)
        await match_events(session, NOW + timedelta(minutes=2))
        await match_events(session, NOW + timedelta(minutes=3))

        matches = list(await session.scalars(select(JobMatch).order_by(JobMatch.occurrence_id)))
        alerts = list(
            await session.scalars(
                select(EmailDelivery)
                .where(EmailDelivery.kind == "alert")
                .order_by(EmailDelivery.created_at)
            )
        )
        repost_occurrence = await session.scalar(
            select(JobOccurrence).where(JobOccurrence.kind == "reposted")
        )
        repost_alert = next(
            item for item in alerts if item.occurrence_ids == [repost_occurrence.id]
        )
        body = await payload(session, repost_alert, user)

        assert len(matches) == 2
        assert [match.event_type for match in matches] == ["discovered", "reposted"]
        assert len(alerts) == 2
        assert len({item.dedupe_key for item in alerts}) == 2
        assert body["subject"].startswith("Reposted:")
        assert "REPOSTED OPPORTUNITY" in body["html"]
        assert "REPOSTED OPPORTUNITY" in body["text"]


async def test_unprocessed_closed_occurrence_does_not_get_alert_after_repost(
    sessions: async_sessionmaker,
) -> None:
    async with sessions() as session, session.begin():
        await seed(session, recap=False)
        await discover(session)
        await repost(session)
        events = list(await session.scalars(select(DiscoveryEvent).order_by(DiscoveryEvent.id)))
        events[1].discovered_at = NOW + timedelta(minutes=1)
        await match_events(session, NOW + timedelta(minutes=2))

        alerts = list(
            await session.scalars(select(EmailDelivery).where(EmailDelivery.kind == "alert"))
        )
        assert len(alerts) == 1
        occurrence = await session.get(JobOccurrence, alerts[0].occurrence_ids[0])
        assert occurrence.kind == "reposted"


async def test_pending_alert_for_closed_occurrence_is_cancelled_after_repost(
    sessions: async_sessionmaker,
) -> None:
    async with sessions() as session, session.begin():
        await seed(session, recap=False)
        posting = await discover(session)
        await match_events(session, NOW)
        old_occurrence = await session.scalar(
            select(JobOccurrence).where(JobOccurrence.kind == "discovered")
        )
        old_alert = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.occurrence_ids == [old_occurrence.id])
        )
        await repost(session)
        repost_event = await session.scalar(
            select(DiscoveryEvent).where(DiscoveryEvent.event_type == "reposted")
        )
        repost_event.discovered_at = NOW + timedelta(minutes=1)
        await match_events(session, NOW + timedelta(minutes=2))
        assert posting.id == old_alert.job_ids[0]
        # The new occurrence alert is accepted to leave only the stale alert claimable.
        latest = await session.scalar(
            select(EmailDelivery)
            .where(EmailDelivery.kind == "alert", EmailDelivery.id != old_alert.id)
            .limit(1)
        )
        latest.status, latest.first_attempt = "accepted", NOW + timedelta(minutes=3)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected send"))
    ) as client:
        worker = NotificationWorker(sessions, client, verify_recipient=AsyncMock(return_value=True))
        assert await worker.claim(NOW + timedelta(minutes=4)) is None

    async with sessions() as session:
        assert await session.get(EmailDelivery, old_alert.id) is not None
        assert (await session.get(EmailDelivery, old_alert.id)).status == "cancelled"


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


async def test_mixed_recap_separates_new_and_reposted_occurrences(
    sessions: async_sessionmaker,
) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        await discover(session, 1)
        await discover(session, 2)
        await match_events(session, NOW)
        await repost(session, 2)
        repost_event = await session.scalar(
            select(DiscoveryEvent).where(DiscoveryEvent.event_type == "reposted")
        )
        repost_event.discovered_at = NOW + timedelta(minutes=1)
        await match_events(session, NOW + timedelta(minutes=2))
        await make_recaps(session, NOW + timedelta(hours=9))

        recap = await session.scalar(select(EmailDelivery).where(EmailDelivery.kind == "recap"))
        body = await payload(session, recap, user)
        assert recap.total_matches == 3
        assert recap.new_count == 2
        assert recap.reposted_count == 1
        assert len(recap.occurrence_ids) == 3
        rows = await occurrence_rows(session, recap.occurrence_ids)
        assert [(row["kind"], row["closed"]) for row in rows if row["id"] == 2] == [
            ("discovered", True),
            ("reposted", False),
        ]
        assert body["subject"] == "Your JobPing recap: 2 new, 1 reposted"
        assert "NEW JOBS · 2" in body["html"]
        assert "REPOSTED JOBS · 1" in body["html"]
        assert "REPOSTED" in body["text"]
        assert "NEW JOBS · 2" in body["text"]
        assert "REPOSTED JOBS · 1" in body["text"]


async def test_recap_with_only_reposts_has_no_new_jobs_section(
    sessions: async_sessionmaker,
) -> None:
    async with sessions() as session, session.begin():
        user = await seed(session)
        user.opted_at = NOW + timedelta(seconds=1)
        await discover(session)
        await match_events(session, NOW)
        user.opted_at = NOW
        await repost(session)
        repost_event = await session.scalar(
            select(DiscoveryEvent).where(DiscoveryEvent.event_type == "reposted")
        )
        repost_event.discovered_at = NOW + timedelta(minutes=1)
        await match_events(session, NOW + timedelta(minutes=2))
        await make_recaps(session, NOW + timedelta(hours=9))
        recap = await session.scalar(select(EmailDelivery).where(EmailDelivery.kind == "recap"))
        body = await payload(session, recap, user)
        assert recap.total_matches == 1
        assert recap.new_count == 0 and recap.reposted_count == 1
        assert "Your JobPing recap: 0 new, 1 reposted" == body["subject"]
        assert "NEW JOBS" not in body["html"]
        assert "REPOSTED JOBS · 1" in body["html"]


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


async def queued_repost(sessions: async_sessionmaker) -> str:
    async with sessions() as session, session.begin():
        await seed(session, recap=False)
        await discover(session)
        await match_events(session, NOW)
        original_alert = await session.scalar(
            select(EmailDelivery).where(EmailDelivery.kind == "alert")
        )
        original_alert.status, original_alert.first_attempt = "accepted", NOW
        await repost(session)
        event = await session.scalar(
            select(DiscoveryEvent).where(DiscoveryEvent.event_type == "reposted")
        )
        event.discovered_at = NOW + timedelta(minutes=1)
        await match_events(session, NOW + timedelta(minutes=2))
        occurrence = await session.scalar(
            select(JobOccurrence).where(JobOccurrence.kind == "reposted")
        )
        return await session.scalar(
            select(EmailDelivery.id).where(
                EmailDelivery.kind == "alert",
                EmailDelivery.occurrence_ids == [occurrence.id],
            )
        )


async def test_send_retries_frozen_payload_after_restart(sessions: async_sessionmaker) -> None:
    item_id = await queued_repost(sessions)
    async with sessions() as session:
        item = await session.get(EmailDelivery, item_id)
        occurrence = await session.get(JobOccurrence, item.occurrence_ids[0])
        assert item.status == "pending"
        assert occurrence.kind == "reposted" and occurrence.closed_at is None
        assert utc(item.created_at) == NOW + timedelta(minutes=2)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("uncertain", request=request)
        return httpx.Response(200, json={"id": "resend-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = NotificationWorker(sessions, client, verify_recipient=AsyncMock(return_value=True))
        assert await worker.send_one(NOW + timedelta(minutes=3))
        async with sessions() as session, session.begin():
            posting = await session.get(JobPosting, 1)
            posting.title = "Changed after request"
        restarted = NotificationWorker(
            sessions, client, verify_recipient=AsyncMock(return_value=True)
        )
        assert await restarted.send_one(NOW + timedelta(minutes=5))
    assert requests[0].content == requests[1].content
    assert json.loads(requests[0].content)["subject"].startswith("Reposted:")
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
        event = await session.scalar(
            select(DiscoveryEvent).where(DiscoveryEvent.job_id == posting.id)
        )
        event.discovered_at = NOW + timedelta(days=1)
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


def test_occurrence_migration_backfills_without_creating_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "occurrence-backfill.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    config = Config("alembic.ini")
    command.upgrade(config, "7a7aec011831")
    connection = sqlite3.connect(database_path)
    connection.execute(
        "INSERT INTO companies (id, name, domain, created_at) VALUES (1, 'Acme', NULL, ?)",
        ("2026-08-01 00:00:00",),
    )
    for job_id in (1, 2, 3):
        connection.execute(
            """INSERT INTO job_postings
            (id, company_id, title, base_hash, content_hash, apply_url, location,
             season, job_type, is_closed, created_at, updated_at, posted_at)
            VALUES (?, 1, ?, ?, ?, ?, 'Remote', 2027, 'internship', ?, ?, ?, ?)""",
            (
                job_id,
                f"Engineer {job_id}",
                f"{job_id:064x}",
                f"{job_id:064x}",
                f"https://example.com/jobs/{job_id}",
                int(job_id == 3),
                "2026-08-01 00:00:00",
                "2026-08-02 00:00:00",
                "2026-08-01 00:00:00",
            ),
        )
    connection.execute(
        """INSERT INTO notification_subscribers
        (id, email, verified, alerts, recap, job_types, seasons, timezone, provider,
         webhook_id, connection_version, status, suppressed, unsubscribe_token)
        VALUES ('alice', 'alice@example.com', 1, 1, 1, ?, ?, 'UTC', 'shared',
                'webhook-alice', 'v1', 'ready', 0, 'unsubscribe-alice')""",
        (json.dumps(["internship", "new_grad"]), json.dumps([2026, 2027])),
    )
    connection.execute(
        "INSERT INTO notification_events (job_id, discovered_at, processed) VALUES (1, ?, 0)",
        ("2026-08-01 00:00:00",),
    )
    connection.execute(
        "INSERT INTO notification_events (job_id, discovered_at, processed) VALUES (2, ?, 1)",
        ("2026-08-01 00:00:00",),
    )
    connection.execute(
        """INSERT INTO notification_matches (id, subscriber_id, job_id, discovered_at)
        VALUES (1, 'alice', 1, ?)""",
        ("2026-08-01 00:00:00",),
    )
    connection.execute(
        """INSERT INTO notification_deliveries
        (id, subscriber_id, kind, dedupe_key, job_ids, created_at, status, due_at, attempts)
        VALUES ('delivery-1', 'alice', 'recap', 'recap:alice:legacy', ?, ?, 'pending', ?, 0)""",
        (
            json.dumps([1, 2]),
            "2026-08-01 00:00:00",
            "2026-08-01 00:00:00",
        ),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")
    connection = sqlite3.connect(database_path)
    occurrences = connection.execute(
        "SELECT id, job_id, kind FROM job_occurrences ORDER BY job_id"
    ).fetchall()
    events = connection.execute(
        "SELECT job_id, occurrence_id, event_type, processed "
        "FROM notification_events ORDER BY job_id"
    ).fetchall()
    matches = connection.execute(
        "SELECT job_id, occurrence_id, event_type FROM notification_matches"
    ).fetchall()
    delivery_row = connection.execute(
        "SELECT occurrence_ids, total_matches, new_count, reposted_count "
        "FROM notification_deliveries WHERE id = 'delivery-1'"
    ).fetchone()
    connection.close()

    assert len(occurrences) == 3
    assert all(row[2] == "discovered" for row in occurrences)
    assert len(events) == 2  # Existing events move; job 3 gets no historical event.
    assert [(row[0], row[2], row[3]) for row in events] == [
        (1, "discovered", 0),
        (2, "discovered", 1),
    ]
    assert len(matches) == 1 and matches[0][0] == 1 and matches[0][2] == "discovered"
    occurrence_ids = json.loads(delivery_row[0])
    assert occurrence_ids == [occurrences[0][0], occurrences[1][0]]
    assert delivery_row[1:] == (2, 2, 0)


async def test_timezone_detection_does_not_overwrite_saved_choice(api: httpx.AsyncClient) -> None:
    first = await api.get("/api/v1/me/notification-settings?timezone=America/Phoenix")
    assert first.json()["timezone"] == "America/Phoenix"
    await api.put("/api/v1/me/notification-settings", json={"timezone": "UTC"})
    again = await api.get("/api/v1/me/notification-settings?timezone=America/Phoenix")
    assert again.json()["timezone"] == "UTC"
