"""Tests for the live WebSocket API route."""

from __future__ import annotations

from typing import Any, cast

import pytest
from app.api.v1 import ws as ws_module
from app.api.v1.websocket_manager import ConnectionManager
from fastapi import WebSocket, WebSocketDisconnect
from pytest import MonkeyPatch


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = iter(messages)
        self.accepted = False
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        try:
            return next(self.messages)
        except StopIteration:
            raise WebSocketDisconnect from None

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


@pytest.mark.asyncio
async def test_live_websocket_handles_keepalive_and_disconnect(
    monkeypatch: MonkeyPatch,
) -> None:
    connections = ConnectionManager()
    websocket = FakeWebSocket(["client-message", " ping "])
    monkeypatch.setattr(ws_module, "manager", connections)

    await ws_module.live_job_feed(cast(WebSocket, websocket))

    assert websocket.accepted is True
    assert websocket.sent == [{"type": "pong"}]
    assert connections.active_count == 0
