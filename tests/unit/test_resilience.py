"""Deterministic tests for transient retry policy."""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest
from app.utils.resilience import retry_async


def timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("temporary", request=httpx.Request("GET", "https://example.test"))


@pytest.mark.asyncio
async def test_success_after_failure_and_metadata_preserved() -> None:
    calls = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    @retry_async(attempts=3, min_delay=1, max_delay=10, jitter=0, sleep=sleep)
    async def operation(value: int) -> int:
        """Documented operation."""
        nonlocal calls
        calls += 1
        if calls < 2:
            raise timeout()
        return value

    assert await operation(7) == 7
    assert delays == [1]
    assert operation.__name__ == "operation"
    assert operation.__doc__ == "Documented operation."


@pytest.mark.asyncio
async def test_exhaustion_reraises_original_exception() -> None:
    @retry_async(attempts=2, min_delay=0, max_delay=0, jitter=0)
    async def operation() -> None:
        raise timeout()

    with pytest.raises(httpx.ReadTimeout, match="temporary"):
        await operation()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bad"), TypeError("bug")])
async def test_programming_and_validation_errors_are_not_retried(error: Exception) -> None:
    calls = 0

    @retry_async(attempts=3)
    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(type(error)):
        await operation()
    assert calls == 1


@pytest.mark.asyncio
async def test_cancellation_is_never_retried() -> None:
    calls = 0

    @retry_async(attempts=3)
    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await operation()
    assert calls == 1


@pytest.mark.asyncio
async def test_jitter_and_retry_after_metadata() -> None:
    delays: list[float] = []

    class Limited(Exception):
        status_code = 429
        retry_after_seconds = 4.0

    async def sleep(delay: float) -> None:
        delays.append(delay)

    calls = 0

    @retry_async(attempts=2, min_delay=1, max_delay=5, jitter=1, sleep=sleep, rng=random.Random(0))
    async def operation() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Limited

    await operation()
    assert delays == [4.0]


@pytest.mark.asyncio
async def test_http_status_retries_only_transient_codes() -> None:
    calls = 0

    @retry_async(attempts=3, min_delay=0, max_delay=0, jitter=0)
    async def operation() -> httpx.Response:
        nonlocal calls
        calls += 1
        response = httpx.Response(
            503 if calls == 1 else 200, request=httpx.Request("GET", "https://example.test")
        )
        response.raise_for_status()
        return response

    assert (await operation()).status_code == 200
    assert calls == 2
