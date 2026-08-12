"""Tests for the Redis-backed server-sent event feed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from app.api.v1.sse import _as_sse, _event_stream, live_feed
from app.events.publisher import DEFAULT_EVENT_CHANNEL
from fastapi import Request
from redis.asyncio import Redis
from sse_starlette import EventSourceResponse, ServerSentEvent


class FakePubSub:
    def __init__(self, messages: list[dict[str, object] | None]) -> None:
        self.messages = iter(messages)
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(
        self, *, ignore_subscribe_messages: bool, timeout: float
    ) -> dict[str, object] | None:
        assert ignore_subscribe_messages is True
        assert timeout > 0
        await asyncio.sleep(0)
        return next(self.messages, None)

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self, pubsub: FakePubSub) -> None:
        self.handle = pubsub

    def pubsub(self) -> FakePubSub:
        return self.handle


class FakeRequest:
    def __init__(self, disconnected: list[bool]) -> None:
        self._states = iter(disconnected)

    async def is_disconnected(self) -> bool:
        return next(self._states, True)


def as_request(request: FakeRequest) -> Request:
    return cast(Request, request)


def as_redis(redis: FakeRedis) -> Redis:
    return cast(Redis, redis)


async def collect(stream: AsyncIterator[ServerSentEvent]) -> list[ServerSentEvent]:
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_event_stream_forwards_json_and_closes_pubsub() -> None:
    envelope: dict[str, Any] = {
        "version": 1,
        "type": "JOB_CREATED",
        "job": {"id": 42, "title": "Software Engineer Intern"},
    }
    pubsub = FakePubSub(
        [
            {"type": "subscribe", "data": 1},
            {"type": "message", "data": json.dumps(envelope).encode()},
        ]
    )

    events = await collect(
        _event_stream(
            as_request(FakeRequest([False, False, False, True])),
            as_redis(FakeRedis(pubsub)),
        )
    )

    assert len(events) == 1
    assert events[0].event == "JOB_CREATED"
    assert events[0].id == "42"
    assert json.loads(cast(str, events[0].data)) == envelope
    assert pubsub.subscribed == [DEFAULT_EVENT_CHANNEL]
    assert pubsub.unsubscribed == [DEFAULT_EVENT_CHANNEL]
    assert pubsub.closed is True


@pytest.mark.asyncio
async def test_event_stream_ignores_malformed_payload() -> None:
    pubsub = FakePubSub([{"type": "message", "data": "not-json"}])

    events = await collect(
        _event_stream(
            as_request(FakeRequest([False, True])),
            as_redis(FakeRedis(pubsub)),
        )
    )

    assert events == []
    assert pubsub.closed is True


@pytest.mark.asyncio
async def test_event_stream_preserves_cancellation_and_closes_pubsub() -> None:
    pubsub = FakePubSub([])
    stream = _event_stream(
        as_request(FakeRequest([False])),
        as_redis(FakeRedis(pubsub)),
    )
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert pubsub.closed is True


def test_as_sse_rejects_non_object_and_missing_type() -> None:
    assert _as_sse("[]") is None
    assert _as_sse('{"job":{"id":1}}') is None


@pytest.mark.asyncio
async def test_live_feed_configures_keepalive_and_cache_headers() -> None:
    response = await live_feed(
        as_request(FakeRequest([True])),
        as_redis(FakeRedis(FakePubSub([]))),
    )

    assert isinstance(response, EventSourceResponse)
    assert response.ping_interval == 15
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
