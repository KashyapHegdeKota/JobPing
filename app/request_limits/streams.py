"""Supervise stream leases and stop abusive inbound WebSocket traffic."""

import asyncio
import time
from collections import deque
from contextlib import suppress

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.request_limits.leases import Lease, StreamLeases


class StreamAbuse(Exception):
    """Inbound traffic exceeded the per-connection message budget."""


async def serve_stream(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    leases: StreamLeases,
    lease: Lease,
) -> None:
    accepted = False
    messages: deque[float] = deque()

    async def guarded_receive() -> Message:
        message = await receive()
        if message["type"] == "websocket.receive":
            value = message.get("bytes") or message.get("text", "").encode()
            now = time.monotonic()
            while messages and messages[0] <= now - 5:
                messages.popleft()
            if len(value) > 4096 or len(messages) >= 10:
                raise StreamAbuse
            messages.append(now)
        return message

    async def tracked_send(message: Message) -> None:
        nonlocal accepted
        if message["type"] in {"websocket.accept", "http.response.start"}:
            accepted = True
        await send(message)

    connection = asyncio.create_task(app(scope, guarded_receive, tracked_send))
    renewal = asyncio.create_task(leases.renew_until_lost(lease))
    try:
        done, _ = await asyncio.wait([connection, renewal], return_when=asyncio.FIRST_COMPLETED)
        if connection in done:
            try:
                await connection
            except StreamAbuse:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
        else:
            if leases.limiter.settings.mode == "shadow":
                await connection
                return
            connection.cancel()
            with suppress(asyncio.CancelledError):
                await connection
            if accepted:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1013})
                else:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
    finally:

        async def drain_and_release() -> None:
            for task in (connection, renewal):
                task.cancel()
            await asyncio.gather(connection, renewal, return_exceptions=True)
            await leases.release(lease)

        cleanup = asyncio.create_task(drain_and_release())
        # ASGI disconnects can use level-triggered AnyIO cancellation. Shield
        # both task draining and release, not just the final Redis operation.
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
