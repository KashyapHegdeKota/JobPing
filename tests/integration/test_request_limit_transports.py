"""Actual ASGI transport admission and cleanup, with no network or live services."""

from collections.abc import AsyncIterator

import pytest
from app.request_limits.middleware import RequestLimitMiddleware
from app.request_limits.scripts import LEASE_ACQUIRE, LEASE_RELEASE, TOKEN_BUCKET
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from tests.unit.request_limit_fakes import limiter, redis_mock


def application(capacity: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.request_limiter = limiter()
    app.state.started = 0
    app.state.closed = 0

    async def evaluate(script: str, *args: object) -> list[int] | int:
        return [1, 0] if script == TOKEN_BUCKET else int(capacity)

    redis_mock(app.state.request_limiter).eval.side_effect = evaluate
    app.add_middleware(RequestLimitMiddleware)

    @app.websocket("/api/v1/ws/live")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        app.state.started += 1
        try:
            while True:
                await ws.send_text(await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            app.state.closed += 1

    @app.get("/api/v1/sse/feed")
    async def stream() -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            app.state.started += 1
            try:
                yield "data: test\n\n"
            finally:
                app.state.closed += 1

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def test_capacity_rejects_both_protocols_before_resource_allocation() -> None:
    app = application(False)
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDenialResponse) as caught,
            client.websocket_connect("/api/v1/ws/live"),
        ):
            raise AssertionError("must not accept")
        assert caught.value.status_code == 429
        assert caught.value.headers["retry-after"] == "60"
        assert client.get("/api/v1/sse/feed").status_code == 429
    assert app.state.started == 0


def test_disconnect_and_completed_sse_release_shared_leases() -> None:
    app = application()
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/ws/live") as socket:
            socket.send_text("hello")
            assert socket.receive_text() == "hello"
        assert client.get("/api/v1/sse/feed").text == "data: test\n\n"
    assert app.state.started == 2 and app.state.closed == 2
    scripts = [call.args[0] for call in redis_mock(app.state.request_limiter).eval.call_args_list]
    assert scripts.count(LEASE_ACQUIRE) == 2 and scripts.count(LEASE_RELEASE) == 2


def test_websocket_message_flood_closes_and_cleans_up() -> None:
    app = application()
    with TestClient(app) as client, client.websocket_connect("/api/v1/ws/live") as socket:
        for _ in range(10):
            socket.send_text("ping")
            assert socket.receive_text() == "ping"
        socket.send_text("overflow")
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_text()
        assert caught.value.code == 1008
    assert app.state.closed == 1
