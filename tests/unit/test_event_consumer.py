from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from app.api.v1.websocket_manager import ConnectionManager
from app.events.consumer import RedisEventConsumer
from redis.asyncio import Redis


class FakePubSub:
    def __init__(self) -> None:
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await self.messages.get()


class FakeRedis:
    def __init__(self, pubsub: FakePubSub) -> None:
        self.value = pubsub
        self.closed = False

    def pubsub(self) -> FakePubSub:
        return self.value

    async def aclose(self) -> None:
        self.closed = True


class RecordingConnections:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.received = asyncio.Event()

    async def broadcast_json(self, message: dict[str, Any]) -> int:
        self.messages.append(message)
        self.received.set()
        return 1


def event_json() -> str:
    return json.dumps(
        {
            "version": 1,
            "type": "JOB_CREATED",
            "occurred_at": "2026-08-12T12:00:00Z",
            "job": {"id": 7, "base_hash": "a" * 64, "title": "Software Engineer Intern"},
        }
    )


def build_consumer(redis: FakeRedis, connections: RecordingConnections) -> RedisEventConsumer:
    return RedisEventConsumer(
        cast(Redis, redis),
        cast(ConnectionManager, connections),
    )


@pytest.mark.asyncio
async def test_start_forwards_valid_json_and_stop_closes_only_pubsub() -> None:
    pubsub = FakePubSub()
    redis = FakeRedis(pubsub)
    connections = RecordingConnections()
    consumer = build_consumer(redis, connections)

    await consumer.start()
    assert consumer.is_running is True
    assert pubsub.subscribed == ["jobping:events"]

    await pubsub.messages.put({"type": "subscribe", "data": 1})
    await pubsub.messages.put({"type": "message", "data": event_json().encode()})
    await asyncio.wait_for(connections.received.wait(), timeout=1)

    assert connections.messages == [
        {
            "version": 1,
            "type": "JOB_CREATED",
            "occurred_at": "2026-08-12T12:00:00Z",
            "job": {"id": 7, "base_hash": "a" * 64, "title": "Software Engineer Intern"},
        }
    ]

    await consumer.stop()
    await consumer.stop()
    assert consumer.is_running is False
    assert pubsub.unsubscribed == ["jobping:events"]
    assert pubsub.closed is True
    assert redis.closed is False


@pytest.mark.asyncio
async def test_invalid_events_are_ignored_without_stopping_listener() -> None:
    pubsub = FakePubSub()
    redis = FakeRedis(pubsub)
    connections = RecordingConnections()
    consumer = build_consumer(redis, connections)
    await consumer.start()

    await pubsub.messages.put({"type": "message", "data": "not-json"})
    await pubsub.messages.put(
        {
            "type": "message",
            "data": event_json().replace('"version": 1', '"version": 2'),
        }
    )
    await pubsub.messages.put({"type": "message", "data": event_json()})
    await asyncio.wait_for(connections.received.wait(), timeout=1)

    assert len(connections.messages) == 1
    assert consumer.is_running is True
    await consumer.stop()


@pytest.mark.asyncio
async def test_context_manager_has_deterministic_lifecycle() -> None:
    pubsub = FakePubSub()
    redis = FakeRedis(pubsub)
    connections = RecordingConnections()
    consumer = build_consumer(redis, connections)

    async with consumer:
        assert consumer.is_running is True

    assert pubsub.closed is True
    assert redis.closed is False


def test_channel_validation() -> None:
    redis = FakeRedis(FakePubSub())
    connections = RecordingConnections()

    with pytest.raises(ValueError, match="channel"):
        RedisEventConsumer(
            cast(Redis, redis), cast(ConnectionManager, connections), channel="bad ch"
        )
