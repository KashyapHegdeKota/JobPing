"""Tests for commit-safe Redis job event publication."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from app.db.models import Base
from app.db.repository import DatabaseRepository
from app.events.publisher import EventPublisher, JobEventType
from app.schemas.job import NormalizedJob
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def make_job(**changes: object) -> NormalizedJob:
    values: dict[str, object] = {
        "company_name": "Acme",
        "title": "Software Engineer Intern",
        "base_hash": "a" * 64,
        "content_hash": "b" * 64,
        "apply_url": "https://example.com/jobs/1",
        "location": "New York, NY",
        "season": 2027,
        "job_type": "internship",
        "is_closed": False,
    }
    values.update(changes)
    return NormalizedJob.model_validate(values)


def publisher_with_mock() -> tuple[EventPublisher, AsyncMock]:
    client = AsyncMock()
    return EventPublisher(cast(Redis, client)), client


async def test_publisher_emits_versioned_serializable_json() -> None:
    publisher, client = publisher_with_mock()
    timestamp = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

    assert await publisher.publish_job_event(
        JobEventType.JOB_CREATED,
        job_id=7,
        base_hash="a" * 64,
        payload={"title": "SWE — Intern", "is_closed": False},
        occurred_at=timestamp,
    )

    channel, raw_message = client.publish.await_args.args
    message = json.loads(raw_message)
    assert channel == "jobping:events"
    assert message == {
        "version": 1,
        "type": "JOB_CREATED",
        "occurred_at": "2026-08-12T12:00:00Z",
        "job": {
            "id": 7,
            "base_hash": "a" * 64,
            "title": "SWE — Intern",
            "is_closed": False,
        },
    }


async def test_redis_failure_is_reported_without_raising(caplog: pytest.LogCaptureFixture) -> None:
    publisher, client = publisher_with_mock()
    client.publish.side_effect = ConnectionError("redis unavailable")

    result = await publisher.publish_job_event(
        JobEventType.JOB_UPDATED, job_id=1, base_hash="a" * 64, payload={}
    )

    assert result is False
    assert "Failed to publish" in caplog.text


async def test_injected_client_is_not_closed() -> None:
    publisher, client = publisher_with_mock()
    await publisher.aclose()
    await publisher.aclose()
    client.aclose.assert_not_awaited()


async def test_repository_publishes_create_update_but_not_no_change(
    session: AsyncSession,
) -> None:
    publisher, client = publisher_with_mock()
    repository = DatabaseRepository(session, publisher)

    await repository.save_job_posting(make_job())
    await repository.wait_for_pending_events()
    await repository.save_job_posting(make_job())
    await repository.wait_for_pending_events()
    await repository.save_job_posting(make_job(content_hash="c" * 64, location="Remote"))
    await repository.wait_for_pending_events()

    assert client.publish.await_count == 2
    messages = [json.loads(call.args[1]) for call in client.publish.await_args_list]
    assert [message["type"] for message in messages] == ["JOB_CREATED", "JOB_UPDATED"]
    assert messages[1]["job"]["location"] == "Remote"


async def test_caller_rollback_never_publishes(session: AsyncSession) -> None:
    publisher, client = publisher_with_mock()
    repository = DatabaseRepository(session, publisher)

    with pytest.raises(RuntimeError, match="abort"):
        async with session.begin():
            await repository.save_job_posting(make_job())
            raise RuntimeError("abort")
    await repository.wait_for_pending_events()

    client.publish.assert_not_awaited()


async def test_caller_commit_publishes_only_after_commit(session: AsyncSession) -> None:
    publisher, client = publisher_with_mock()
    repository = DatabaseRepository(session, publisher)

    async with session.begin():
        await repository.save_job_posting(make_job())
        client.publish.assert_not_awaited()
    await repository.wait_for_pending_events()

    client.publish.assert_awaited_once()
