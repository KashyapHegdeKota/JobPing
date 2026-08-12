"""Tests for FastAPI configuration and resource ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from app.main import _as_async_database_url, create_app
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class FakeRedis:
    def __init__(self, pubsub: FakePubSub | None = None) -> None:
        self.closed = False
        self.pubsub_handle = pubsub or FakePubSub()

    def pubsub(self) -> FakePubSub:
        return self.pubsub_handle

    async def aclose(self) -> None:
        self.closed = True


class FakePubSub:
    def __init__(self, *, subscribe_error: BaseException | None = None) -> None:
        self.subscribe_error = subscribe_error
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)
        if self.subscribe_error is not None:
            raise self.subscribe_error

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        await asyncio.Event().wait()
        yield {}


def test_lifespan_stores_and_closes_owned_resources() -> None:
    engine = FakeEngine()
    redis = FakeRedis()
    application = create_app(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://example.invalid/0",
        startup_health_checks=False,
        engine_factory=lambda _: cast(AsyncEngine, engine),
        redis_factory=lambda _: cast(Redis, redis),
    )

    with TestClient(application):
        assert application.state.db_engine is engine
        assert application.state.redis is redis
        assert application.state.db_sessionmaker is not None
        assert application.state.redis_event_consumer.is_running
        assert application.state.redis_event_listener_task is not None
        assert application.state.redis_event_listener_task.done() is False
        assert redis.pubsub_handle.subscribed == ["jobping:events"]
        assert not engine.disposed
        assert not redis.closed

    assert redis.pubsub_handle.unsubscribed == ["jobping:events"]
    assert redis.pubsub_handle.closed
    assert engine.disposed
    assert redis.closed


def test_listener_start_failure_still_closes_owned_resources() -> None:
    engine = FakeEngine()
    pubsub = FakePubSub(subscribe_error=RuntimeError("subscribe failed"))
    redis = FakeRedis(pubsub)
    application = create_app(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://example.invalid/0",
        startup_health_checks=False,
        engine_factory=lambda _: cast(AsyncEngine, engine),
        redis_factory=lambda _: cast(Redis, redis),
    )

    try:
        with TestClient(application):
            raise AssertionError("lifespan startup should fail")
    except RuntimeError as error:
        assert str(error) == "subscribe failed"

    assert pubsub.closed
    assert redis.closed
    assert engine.disposed


def test_cors_wildcard_never_enables_credentials() -> None:
    application = create_app(cors_origins=["*"], startup_health_checks=False)
    middleware = next(
        item for item in application.user_middleware if item.cls.__name__ == "CORSMiddleware"
    )

    assert middleware.kwargs["allow_origins"] == ["*"]
    assert middleware.kwargs["allow_credentials"] is False


def test_database_url_normalization_preserves_async_urls() -> None:
    assert (
        _as_async_database_url("postgresql://user:pass@db/jobping")
        == "postgresql+psycopg://user:pass@db/jobping"
    )
    assert _as_async_database_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"
