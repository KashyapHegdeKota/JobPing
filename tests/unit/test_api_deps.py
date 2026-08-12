"""Lifecycle tests for FastAPI database and Redis dependencies."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from app.api.deps import get_db, get_redis
from fastapi import HTTPException, Request
from redis.asyncio import Redis


class FakeTransaction:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session.transaction_started = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        if exc_type is None:
            self._session.committed = True
        else:
            self._session.rolled_back = True


class FakeSession:
    def __init__(self) -> None:
        self.transaction_started = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self._session.closed = True


class FakeSessionmaker:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    def __call__(self) -> FakeSessionContext:
        return FakeSessionContext(self._session)


def make_request(**state: object) -> Request:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))
    return cast(Request, request)


async def test_get_db_commits_and_closes_after_success() -> None:
    session = FakeSession()
    dependency = get_db(make_request(db_sessionmaker=FakeSessionmaker(session)))

    assert await anext(dependency) is session
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)

    assert session.transaction_started is True
    assert session.committed is True
    assert session.rolled_back is False
    assert session.closed is True


async def test_get_db_rolls_back_closes_and_preserves_route_exception() -> None:
    session = FakeSession()
    dependency = get_db(make_request(db_sessionmaker=FakeSessionmaker(session)))
    await anext(dependency)

    with pytest.raises(RuntimeError, match="route failed"):
        await dependency.athrow(RuntimeError("route failed"))

    assert session.rolled_back is True
    assert session.committed is False
    assert session.closed is True


async def test_get_db_preserves_cancellation_and_cleans_up() -> None:
    session = FakeSession()
    dependency = get_db(make_request(db_sessionmaker=FakeSessionmaker(session)))
    await anext(dependency)

    with pytest.raises(asyncio.CancelledError):
        await dependency.athrow(asyncio.CancelledError())

    assert session.rolled_back is True
    assert session.closed is True


async def test_get_db_reports_missing_lifespan_resource() -> None:
    dependency = get_db(make_request())

    with pytest.raises(HTTPException) as caught:
        await anext(dependency)

    assert caught.value.status_code == 503
    assert "startup" in caught.value.detail


async def test_get_redis_returns_shared_client_without_closing_it() -> None:
    client = SimpleNamespace(aclose_called=False)

    assert await get_redis(make_request(redis=client)) is cast(Redis, client)
    assert client.aclose_called is False


async def test_get_redis_reports_missing_lifespan_resource() -> None:
    with pytest.raises(HTTPException) as caught:
        await get_redis(make_request())

    assert caught.value.status_code == 503
    assert "startup" in caught.value.detail
