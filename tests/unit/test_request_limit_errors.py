"""WebSocket handshakes can be denied without accepting a counted connection."""

from unittest.mock import AsyncMock

from app.request_limits.errors import protection_error_handler, reject, rejection


async def test_websocket_denial_extension_and_fallback() -> None:
    send = AsyncMock()
    await reject(
        {"type": "websocket", "extensions": {"websocket.http.response": {}}},
        AsyncMock(),
        send,
        429,
        5,
    )
    assert send.call_args_list[0].args[0]["type"] == "websocket.http.response.start"
    assert send.call_args_list[0].args[0]["status"] == 429
    assert send.call_args_list[1].args[0]["type"] == "websocket.http.response.body"
    send.reset_mock()
    await reject({"type": "websocket"}, AsyncMock(), send, 503)
    send.assert_awaited_once_with({"type": "websocket.close", "code": 1008})


async def test_private_exception_response_contract() -> None:
    result = await protection_error_handler(AsyncMock(), rejection(429, 7))
    assert result.status_code == 429
    assert result.headers["retry-after"] == "7"
    assert b'"code":"rate_limited"' in result.body
