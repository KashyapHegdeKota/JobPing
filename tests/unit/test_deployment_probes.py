"""Deployment readiness checks actual owned dependencies and redacts failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from app.main import create_app
from fastapi.testclient import TestClient


def probes() -> TestClient:
    app = create_app(startup_health_checks=False)
    app.state.redis_event_consumer = Mock(is_running=True)
    connection = AsyncMock()
    app.state.db_engine = Mock()
    app.state.db_engine.connect.return_value = connection
    app.state.redis = AsyncMock()
    return TestClient(app)


def test_liveness_and_exact_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_GIT_SHA", "a" * 40)
    monkeypatch.setenv("APP_COLOR", "green")
    client = probes()
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/version").json() == {"commit": "a" * 40, "color": "green"}
    assert client.get("/ready").json() == {"status": "ready"}
    client.app.state.redis.ping.assert_awaited_once()


@pytest.mark.parametrize("dependency", ["database", "redis", "listener", "limiter"])
def test_dependency_failure_is_unready_and_redacted(dependency: str) -> None:
    client = probes()
    state = client.app.state
    if dependency == "database":
        state.db_engine.connect.side_effect = RuntimeError("secret-database-credential")
    elif dependency == "redis":
        state.redis.ping.side_effect = RuntimeError("secret-redis-credential")
    elif dependency == "limiter":
        state.request_limiter = Mock()
        state.request_limiter.settings.mode = "off"
        state.request_limiter.redis = AsyncMock()
        state.request_limiter.redis.ping.side_effect = RuntimeError("secret-limiter-credential")
    else:
        state.redis_event_consumer.is_running = False
    response = client.get("/ready")
    assert response.status_code == 503
    assert "secret" not in response.text
