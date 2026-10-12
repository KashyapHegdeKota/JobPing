"""Protocol-level substitutes; real algorithm correctness uses isolated Redis."""

from typing import cast
from unittest.mock import AsyncMock

from app.request_limits.limiter import RequestLimiter
from app.request_limits.settings import LimitSettings
from redis.asyncio import Redis


def limiter(mode: str = "enforce", **settings: object) -> RequestLimiter:
    redis = AsyncMock(spec=Redis)
    redis.eval = AsyncMock(return_value=[1, 0])
    return RequestLimiter(redis, LimitSettings(mode=mode, secret="s" * 32, **settings))  # type: ignore[arg-type]


def redis_mock(value: RequestLimiter) -> AsyncMock:
    return cast(AsyncMock, value.redis)
