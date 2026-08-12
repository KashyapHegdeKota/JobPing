"""Concurrency-safe registry and broadcaster for live WebSocket clients."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Track accepted WebSockets and broadcast without holding the registry lock."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        """Return the current connection count for health and test diagnostics."""
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a WebSocket.

        Acceptance happens before registration so a failed handshake can never
        leave a dead connection in the registry.
        """
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket; repeated disconnect notifications are harmless."""
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast_json(self, message: dict[str, Any]) -> int:
        """Send a JSON event concurrently and evict connections that fail.

        A snapshot avoids holding the registry lock across network I/O. The
        returned count represents successful deliveries.
        """
        async with self._lock:
            connections = tuple(self._connections)
        if not connections:
            return 0

        results = await asyncio.gather(
            *(connection.send_json(message) for connection in connections),
            return_exceptions=True,
        )
        failed = {
            connection
            for connection, result in zip(connections, results, strict=True)
            if isinstance(result, BaseException)
        }
        if failed:
            async with self._lock:
                self._connections.difference_update(failed)
            for connection, result in zip(connections, results, strict=True):
                if connection in failed:
                    logger.warning(
                        "Dropping WebSocket after broadcast failure: %s",
                        type(result).__name__,
                    )
        return len(connections) - len(failed)


manager = ConnectionManager()

__all__ = ["ConnectionManager", "manager"]
