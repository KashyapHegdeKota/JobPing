"""Redis Pub/Sub publisher for committed job changes."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from redis.asyncio import Redis

DEFAULT_EVENT_CHANNEL = "jobping:events"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
EVENT_SCHEMA_VERSION = 1

logger = logging.getLogger(__name__)


class JobEventType(StrEnum):
    """Externally visible job lifecycle events."""

    JOB_CREATED = "JOB_CREATED"
    JOB_UPDATED = "JOB_UPDATED"


class EventPublisher:
    """Publish versioned JSON events without taking ownership of injected clients.

    Redis delivery is best-effort after the database commit. A Redis outage is
    logged and reported to the caller, but can never roll back an already durable
    SQL transaction.
    """

    def __init__(
        self,
        client: Redis,
        *,
        channel: str = DEFAULT_EVENT_CHANNEL,
        owns_client: bool = False,
    ) -> None:
        normalized_channel = channel.strip()
        if not normalized_channel or any(character.isspace() for character in normalized_channel):
            raise ValueError("channel must be non-empty and contain no whitespace")
        self._client = client
        self._channel = normalized_channel
        self._owns_client = owns_client
        self._closed = False

    @classmethod
    def from_url(cls, url: str | None = None, *, channel: str = DEFAULT_EVENT_CHANNEL) -> Self:
        """Build a publisher that owns its Redis connection pool."""
        redis_url = url if url is not None else os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        if not redis_url.strip():
            raise ValueError("Redis URL must be non-empty")
        return cls(
            Redis.from_url(redis_url, decode_responses=True), channel=channel, owns_client=True
        )

    async def publish_job_event(
        self,
        event_type: JobEventType,
        *,
        job_id: int,
        base_hash: str,
        payload: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> bool:
        """Publish one stable, JSON-serializable event envelope."""
        if self._closed:
            raise RuntimeError("EventPublisher is closed")
        timestamp = occurred_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        envelope = {
            "version": EVENT_SCHEMA_VERSION,
            "type": event_type.value,
            "occurred_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "job": {"id": job_id, "base_hash": base_hash, **payload},
        }
        message = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        try:
            await self._client.publish(self._channel, message)
        except Exception:
            logger.exception("Failed to publish %s for job_id=%s", event_type, job_id)
            return False
        return True

    async def aclose(self) -> None:
        """Close only a client created by :meth:`from_url`."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()


__all__ = [
    "DEFAULT_EVENT_CHANNEL",
    "EVENT_SCHEMA_VERSION",
    "EventPublisher",
    "JobEventType",
]
