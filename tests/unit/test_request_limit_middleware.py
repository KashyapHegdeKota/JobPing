"""Ingress rejection precedes business work and works with CORS and streaming."""

from collections.abc import AsyncIterator

import httpx
import pytest
from app.request_limits.middleware import RequestLimitMiddleware
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.exceptions import ConnectionError

from tests.unit.request_limit_fakes import limiter, redis_mock


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.state.request_limiter = limiter()
    app.state.calls = 0
    app.add_middleware(RequestLimitMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=["https://jobping.website"], expose_headers=["Retry-After"]
    )

    @app.get("/api/v1/{path:path}")
    async def endpoint(path: str) -> dict:
        app.state.calls += 1
        return {"path": path}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://test"
    ) as result:
        result.test_app = app
        yield result


async def test_rate_rejection_before_handler_and_cors(client: httpx.AsyncClient) -> None:
    app = client.test_app
    redis_mock(app.state.request_limiter).eval.return_value = [0, 4]
    result = await client.get("/api/v1/jobs", headers={"Origin": "https://jobping.website"})
    assert result.status_code == 429
    assert result.json()["code"] == "rate_limited"
    assert result.headers["retry-after"] == "4"
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["access-control-allow-origin"] == "https://jobping.website"
    assert "Retry-After" in result.headers["access-control-expose-headers"]
    assert app.state.calls == 0


async def test_forged_forwarding_header_does_not_change_key(client: httpx.AsyncClient) -> None:
    app = client.test_app
    await client.get("/api/v1/jobs", headers={"X-Forwarded-For": "192.0.2.5"})
    first = redis_mock(app.state.request_limiter).eval.call_args_list[1].args[2]
    await client.get("/api/v1/jobs", headers={"X-Forwarded-For": "192.0.2.6"})
    second = redis_mock(app.state.request_limiter).eval.call_args_list[4].args[2]
    assert first == second


async def test_fail_closed_and_shadow(client: httpx.AsyncClient) -> None:
    app = client.test_app
    redis_mock(app.state.request_limiter).eval.side_effect = ConnectionError("secret")
    result = await client.get("/api/v1/jobs")
    assert result.status_code == 503 and "secret" not in result.text
    assert app.state.calls == 0
    app.state.request_limiter = limiter("shadow")
    redis_mock(app.state.request_limiter).eval.side_effect = ConnectionError()
    assert (await client.get("/api/v1/jobs")).status_code == 200


async def test_cache_hits_still_spend_quota(client: httpx.AsyncClient) -> None:
    app = client.test_app
    for _ in range(2):
        assert (await client.get("/api/v1/companies")).status_code == 200
    assert app.state.calls == 1
    assert redis_mock(app.state.request_limiter).eval.call_count == 6
    await client.get("/api/v1/companies?query=different")
    await client.get("/api/v1/companies", headers={"Authorization": "Bearer private"})
    assert app.state.calls == 3


async def test_internal_health_only_and_denied_origin(client: httpx.AsyncClient) -> None:
    app = client.test_app
    redis_mock(app.state.request_limiter).eval.return_value = [0, 1]
    assert (await client.get("/api/v1/health")).status_code == 200
    result = await client.get("/api/v1/stats", headers={"Origin": "https://evil.test"})
    assert result.status_code == 429
    assert "access-control-allow-origin" not in result.headers
