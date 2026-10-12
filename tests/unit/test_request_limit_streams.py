"""Connection cancellation, oversized frames and renewal failures release leases."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from app.request_limits.leases import Lease
from app.request_limits.streams import serve_stream

from tests.unit.request_limit_fakes import limiter


async def test_stream_renewal_loss_closes_and_releases() -> None:
    leases = AsyncMock()
    leases.limiter = limiter()
    leases.renew_until_lost.return_value = None
    cleaned = asyncio.Event()

    async def app(scope: dict, receive: object, send: object) -> None:
        try:
            await send({"type": "websocket.accept"})
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    send = AsyncMock()
    lease = Lease(["ip", "global"], "token")
    await serve_stream(app, {"type": "websocket"}, AsyncMock(), send, leases, lease)
    assert cleaned.is_set()
    assert send.call_args.args[0] == {"type": "websocket.close", "code": 1013}
    leases.release.assert_awaited_once_with(lease)


async def test_oversized_frame_closes_before_handler_reads_data() -> None:
    leases = AsyncMock()

    async def wait(lease: object) -> None:
        await asyncio.Event().wait()

    leases.renew_until_lost.side_effect = wait

    async def app(scope: dict, receive: object, send: object) -> None:
        await send({"type": "websocket.accept"})
        await receive()
        raise AssertionError("must reject oversized frame")

    receive = AsyncMock(return_value={"type": "websocket.receive", "text": "x" * 4097})
    send = AsyncMock()
    await serve_stream(app, {"type": "websocket"}, receive, send, leases, Lease([], "x"))
    assert send.call_args.args[0]["code"] == 1008
    leases.release.assert_awaited_once()


async def test_connection_cancellation_drains_owned_tasks() -> None:
    leases = AsyncMock()
    started = asyncio.Event()

    async def wait(*args: object) -> None:
        started.set()
        await asyncio.Event().wait()

    leases.renew_until_lost.side_effect = wait
    task = asyncio.create_task(
        serve_stream(wait, {"type": "http"}, AsyncMock(), AsyncMock(), leases, Lease([], "x"))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    leases.release.assert_awaited_once()
