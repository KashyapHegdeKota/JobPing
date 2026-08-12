"""WebSocket endpoint for real-time job event delivery."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.v1.websocket_manager import manager

router = APIRouter(tags=["live"])


@router.websocket("/api/v1/ws/live")
async def live_job_feed(websocket: WebSocket) -> None:
    """Keep a client registered for broadcasts until it disconnects.

    Redis events are pushed by the shared connection manager. Incoming text is
    otherwise ignored, except for a lightweight application-level keepalive so
    browser clients can confirm that the connection is still responsive.
    """
    await manager.connect(websocket)
    try:
        while True:
            message = await websocket.receive_text()
            if message.strip().casefold() == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(websocket)


__all__ = ["router"]
