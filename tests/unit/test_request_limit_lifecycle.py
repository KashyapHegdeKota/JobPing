"""The API lifespan owns the separate bounded limiter Redis connection pool."""

from typing import cast

from app.main import create_app
from app.request_limits.settings import LimitSettings
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.unit.test_main import FakeEngine, FakeRedis


def test_owned_limiter_pool_and_internal_health() -> None:
    engine, events, counters = FakeEngine(), FakeRedis(), FakeRedis()
    app = create_app(
        startup_health_checks=False,
        engine_factory=lambda _: cast(AsyncEngine, engine),
        redis_factory=lambda _: cast(Redis, events),
        limiter_redis_factory=lambda _: cast(Redis, counters),
        limit_settings=LimitSettings(mode="enforce", secret="s" * 32),
    )
    with TestClient(app, client=("127.0.0.1", 1)) as client:
        assert app.state.request_limiter.redis is counters
        assert client.get("/api/v1/health").json() == {"status": "ready"}
    assert events.closed and counters.closed and engine.disposed


def test_incomplete_lifespan_does_not_bypass_configured_protection() -> None:
    app = create_app(limit_settings=LimitSettings(mode="enforce", secret="s" * 32))
    # No context manager means no startup and no real service connection.
    client = TestClient(app)
    response = client.get("/api/v1/jobs")
    assert response.status_code == 503
    assert response.json()["code"] == "temporarily_unavailable"
