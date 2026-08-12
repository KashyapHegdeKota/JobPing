"""Typed retry policy for transient external I/O failures."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

P = ParamSpec("P")
R = TypeVar("R")
Sleep = Callable[[float], Awaitable[None]]
_TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


def is_transient(exc: BaseException) -> bool:
    """Return whether an exception represents retryable external I/O."""
    if isinstance(exc, asyncio.CancelledError):
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, PlaywrightTimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    if getattr(exc, "retryable", False) is True:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status in _TRANSIENT_STATUS


def retry_async(
    *,
    attempts: int = 3,
    min_delay: float = 0.25,
    max_delay: float = 5.0,
    jitter: float = 0.25,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Retry transient async I/O with exponential backoff and bounded jitter."""
    if attempts < 1 or min_delay < 0 or max_delay < min_delay or jitter < 0:
        raise ValueError("invalid retry policy")
    randomizer = rng or random.Random()

    def wait(state: RetryCallState) -> float:
        outcome = state.outcome
        exc = outcome.exception() if outcome is not None and outcome.failed else None
        retry_after = getattr(exc, "retry_after_seconds", None)
        if isinstance(retry_after, (int, float)) and retry_after >= 0:
            return min(max_delay, float(retry_after))
        delay = min(max_delay, min_delay * (2 ** max(0, state.attempt_number - 1)))
        return min(max_delay, delay + randomizer.uniform(0, jitter))

    def decorate(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            retrying = AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait,
                retry=retry_if_exception(is_transient),
                sleep=sleep,
                reraise=True,
            )
            async for attempt in retrying:
                with attempt:
                    return await function(*args, **kwargs)
            raise RuntimeError("retry loop exited unexpectedly")

        return wrapped

    return decorate


external_retry = retry_async()

__all__ = ["external_retry", "is_transient", "retry_async"]
