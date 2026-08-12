"""Redis Pub/Sub consumer for validated real-time job events."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.api.v1.websocket_manager import ConnectionManager
from app.events.publisher import DEFAULT_EVENT_CHANNEL, EVENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)


class _EventJob(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(gt=0)
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class _EventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    type: Literal["JOB_CREATED", "JOB_UPDATED"]
    occurred_at: datetime
    job: _EventJob

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class RedisEventConsumer:
    """Forward validated Redis events to a WebSocket connection manager.

    The injected Redis client remains owned by the application lifespan. This
    consumer creates and deterministically closes only its Pub/Sub handle.
    """

    def __init__(
        self,
        redis: Redis,
        connections: ConnectionManager,
        *,
        channel: str = DEFAULT_EVENT_CHANNEL,
    ) -> None:
        normalized_channel = channel.strip()
        if not normalized_channel or any(character.isspace() for character in normalized_channel):
            raise ValueError("channel must be non-empty and contain no whitespace")
        self._redis = redis
        self._connections = connections
        self._channel = normalized_channel
        self._task: asyncio.Task[None] | None = None
        self._pubsub: PubSub | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Subscribe before returning, then run the listener in the background."""
        async with self._lifecycle_lock:
            if self.is_running:
                return
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(self._channel)
            except BaseException:
                await pubsub.aclose()
                raise
            self._pubsub = pubsub
            self._task = asyncio.create_task(
                self._listen(pubsub), name=f"redis-events:{self._channel}"
            )

    async def stop(self) -> None:
        """Cancel and drain the listener; safe to call more than once."""
        async with self._lifecycle_lock:
            task = self._task
            if task is None:
                return
            self._task = None
            pubsub = self._pubsub
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if pubsub is not None and self._pubsub is pubsub:
            await self._close_pubsub(pubsub)

    async def run(self) -> None:
        """Run in the current task until cancelled.

        Use this method with a task group when another component owns task
        creation. ``start``/``stop`` provide the equivalent owned-task API.
        """
        async with self._lifecycle_lock:
            if self._task is not None or self._pubsub is not None:
                raise RuntimeError("RedisEventConsumer is already running")
            pubsub = self._redis.pubsub()
            try:
                await pubsub.subscribe(self._channel)
            except BaseException:
                await pubsub.aclose()
                raise
            self._pubsub = pubsub
        await self._listen(pubsub)

    async def _listen(self, pubsub: PubSub) -> None:
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                await self._forward(message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis event listener stopped unexpectedly")
            raise
        finally:
            await self._close_pubsub(pubsub)

    async def _close_pubsub(self, pubsub: PubSub) -> None:
        try:
            await pubsub.unsubscribe(self._channel)
        finally:
            await pubsub.aclose()
            if self._pubsub is pubsub:
                self._pubsub = None

    async def _forward(self, raw_data: object) -> None:
        try:
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode("utf-8")
            if not isinstance(raw_data, str):
                raise TypeError("event data must be JSON text")
            decoded = json.loads(raw_data)
            event = _EventEnvelope.model_validate(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValidationError, ValueError):
            logger.warning("Ignoring invalid event on Redis channel %s", self._channel)
            return
        await self._connections.broadcast_json(event.model_dump(mode="json"))

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.stop()


assert EVENT_SCHEMA_VERSION == 1, "consumer schema must track publisher schema"

__all__ = ["RedisEventConsumer"]
