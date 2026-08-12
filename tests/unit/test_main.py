"""Tests for FastAPI configuration and resource ownership."""

from __future__ import annotations

from typing import cast

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
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


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
        assert not engine.disposed
        assert not redis.closed

    assert engine.disposed
    assert redis.closed


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
