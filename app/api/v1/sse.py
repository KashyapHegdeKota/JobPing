"""Server-sent events fallback for real-time job notifications."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from sse_starlette import EventSourceResponse, ServerSentEvent

from app.api.deps import get_redis
from app.events.publisher import DEFAULT_EVENT_CHANNEL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sse", tags=["live"])

POLL_TIMEOUT_SECONDS = 1.0
KEEPALIVE_SECONDS = 15


def _as_sse(raw_data: object) -> ServerSentEvent | None:
    """Convert a Redis payload into an SSE frame, rejecting malformed events."""
    try:
        if isinstance(raw_data, bytes):
            raw_data = raw_data.decode("utf-8")
        if not isinstance(raw_data, str):
            raise TypeError("event data must be JSON text")
        payload = json.loads(raw_data)
        if not isinstance(payload, dict):
            raise TypeError("event data must be a JSON object")
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event type is required")
        job = payload.get("job")
        job_id = job.get("id") if isinstance(job, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Ignoring invalid event on Redis channel %s", DEFAULT_EVENT_CHANNEL)
        return None

    return ServerSentEvent(
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        event=event_type,
        id=str(job_id) if job_id is not None else None,
    )


async def _event_stream(
    request: Request,
    redis: Redis,
    *,
    channel: str = DEFAULT_EVENT_CHANNEL,
) -> AsyncIterator[ServerSentEvent]:
    """Subscribe one HTTP stream to Redis and release its handle on exit."""
    pubsub: PubSub = redis.pubsub()
    try:
        await pubsub.subscribe(channel)
        while not await request.is_disconnected():
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=POLL_TIMEOUT_SECONDS,
            )
            if message is None or message.get("type") != "message":
                continue
            event = _as_sse(message.get("data"))
            if event is not None:
                yield event
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await pubsub.unsubscribe(channel)
        finally:
            await pubsub.aclose()


@router.get("/feed", response_class=EventSourceResponse)
async def live_feed(
    request: Request,
    redis: Annotated[Redis, Depends(get_redis)],
) -> EventSourceResponse:
    """Stream committed job events, with heartbeat frames for idle clients."""
    return EventSourceResponse(
        _event_stream(request, redis),
        ping=KEEPALIVE_SECONDS,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["live_feed", "router"]
