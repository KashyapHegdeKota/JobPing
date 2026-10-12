"""Uniform non-cacheable HTTP rejections and safe WebSocket handshake denial."""

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send


class RequestProtectionError(HTTPException):
    """Marker for consistent application-level quota rejection responses."""


def rejection(status: int, retry: int = 1) -> RequestProtectionError:
    return RequestProtectionError(
        status,
        detail=(
            "Too many requests. Please wait and try again."
            if status == 429
            else "Request protection is temporarily unavailable. Please try again shortly."
        ),
        headers={"Retry-After": str(max(1, retry)), "Cache-Control": "no-store"},
    )


async def protection_error_handler(request: Request, error: RequestProtectionError) -> JSONResponse:
    return JSONResponse(
        {
            "detail": error.detail,
            "code": "rate_limited" if error.status_code == 429 else "temporarily_unavailable",
        },
        status_code=error.status_code,
        headers=error.headers,
    )


async def reject(scope: Scope, receive: Receive, send: Send, status: int, retry: int = 1) -> None:
    error = rejection(status, retry)
    response = JSONResponse(
        {
            "detail": error.detail,
            "code": "rate_limited" if status == 429 else "temporarily_unavailable",
        },
        status_code=status,
        headers=error.headers,
    )
    if scope["type"] == "http":
        await response(scope, receive, send)
    elif "websocket.http.response" in scope.get("extensions", {}):
        # Starlette emits websocket.http.response.* for a websocket scope.
        await response(scope, receive, send)
    else:
        await send({"type": "websocket.close", "code": 1008})
