"""Ordered, failure-isolated concurrent scraper execution."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
ScraperCall = Callable[[], Awaitable[T]]


@dataclass(frozen=True, slots=True)
class ScraperExecution[T]:
    """Typed outcome for one scraper in input order."""

    index: int
    value: T | None = None
    error: Exception | None = None
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.error is None


async def run_scrapers_concurrently[T](
    scrapers: Sequence[ScraperCall[T]],
    *,
    timeout: float | None = None,
    logger: logging.Logger | None = None,
) -> tuple[ScraperExecution[T], ...]:
    """Run all scraper callables concurrently and preserve input ordering.

    Individual exceptions and timeouts become outcomes. Cancellation of the
    orchestrating caller cancels and drains every child before propagating.
    """
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive")
    log = logger or logging.getLogger(__name__)

    async def invoke(call: ScraperCall[T]) -> T:
        operation = call()
        return (
            await asyncio.wait_for(operation, timeout) if timeout is not None else await operation
        )

    tasks = [
        asyncio.create_task(invoke(call), name=f"scraper-{index}")
        for index, call in enumerate(scrapers)
    ]
    try:
        raw = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    results: list[ScraperExecution[T]] = []
    for index, item in enumerate(raw):
        if isinstance(item, asyncio.CancelledError):
            raise item
        if isinstance(item, Exception):
            timed_out = isinstance(item, TimeoutError)
            log.warning(
                "scraper execution failed",
                extra={
                    "scraper_index": index,
                    "error_type": type(item).__name__,
                    "timed_out": timed_out,
                },
            )
            results.append(ScraperExecution(index=index, error=item, timed_out=timed_out))
        else:
            results.append(ScraperExecution(index=index, value=item))
    return tuple(results)


__all__ = ["ScraperCall", "ScraperExecution", "run_scrapers_concurrently"]
