from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from app.api.v1.websocket_manager import ConnectionManager
from fastapi import WebSocket


class FakeWebSocket:
    def __init__(self, *, accept_error: Exception | None = None) -> None:
        self.accept_error = accept_error
        self.accepted = False
        self.messages: list[dict[str, Any]] = []
        self.send_error: Exception | None = None

    async def accept(self) -> None:
        if self.accept_error is not None:
            raise self.accept_error
        self.accepted = True

    async def send_json(self, message: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        if self.send_error is not None:
            raise self.send_error
        self.messages.append(message)


def as_websocket(value: FakeWebSocket) -> WebSocket:
    return cast(WebSocket, value)


@pytest.mark.asyncio
async def test_connect_broadcast_disconnect_and_idempotency() -> None:
    manager = ConnectionManager()
    first = FakeWebSocket()
    second = FakeWebSocket()

    await asyncio.gather(
        manager.connect(as_websocket(first)),
        manager.connect(as_websocket(second)),
    )
    delivered = await manager.broadcast_json({"type": "JOB_CREATED"})

    assert first.accepted is True
    assert second.accepted is True
    assert delivered == 2
    assert manager.active_count == 2
    assert first.messages == [{"type": "JOB_CREATED"}]
    assert second.messages == [{"type": "JOB_CREATED"}]

    await manager.disconnect(as_websocket(first))
    await manager.disconnect(as_websocket(first))
    assert manager.active_count == 1


@pytest.mark.asyncio
async def test_failed_accept_is_not_registered() -> None:
    manager = ConnectionManager()
    socket = FakeWebSocket(accept_error=RuntimeError("handshake failed"))

    with pytest.raises(RuntimeError, match="handshake failed"):
        await manager.connect(as_websocket(socket))

    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_broadcast_evicts_only_failed_connections() -> None:
    manager = ConnectionManager()
    healthy = FakeWebSocket()
    failed = FakeWebSocket()
    failed.send_error = RuntimeError("closed")
    await manager.connect(as_websocket(healthy))
    await manager.connect(as_websocket(failed))

    assert await manager.broadcast_json({"version": 1}) == 1
    assert manager.active_count == 1
    assert await manager.broadcast_json({"version": 2}) == 1
    assert healthy.messages == [{"version": 1}, {"version": 2}]
