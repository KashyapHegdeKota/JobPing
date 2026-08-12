"""Tests for isolated parallel scraper execution."""

from __future__ import annotations

import asyncio

import pytest
from app.scheduler_parallel import run_scrapers_concurrently


@pytest.mark.asyncio
async def test_mixed_results_preserve_input_order() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.01)
        return "first"

    async def broken() -> str:
        raise RuntimeError("source down")

    async def fast() -> str:
        return "third"

    results = await run_scrapers_concurrently([slow, broken, fast])
    assert [result.index for result in results] == [0, 1, 2]
    assert results[0].value == "first"
    assert isinstance(results[1].error, RuntimeError)
    assert results[2].value == "third"


@pytest.mark.asyncio
async def test_per_scraper_timeout_does_not_block_success() -> None:
    stopped = asyncio.Event()

    async def hanging() -> str:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return "never"

    async def success() -> str:
        return "ok"

    results = await run_scrapers_concurrently([hanging, success], timeout=0.01)
    assert results[0].timed_out
    assert results[1].value == "ok"
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_drains_children() -> None:
    started = asyncio.Event()
    stopped = 0

    async def hanging() -> None:
        nonlocal stopped
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped += 1

    parent = asyncio.create_task(run_scrapers_concurrently([hanging, hanging]))
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert stopped == 2
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("scraper-")]


@pytest.mark.asyncio
async def test_empty_input_and_invalid_timeout() -> None:
    assert await run_scrapers_concurrently([]) == ()
    with pytest.raises(ValueError, match="positive"):
        await run_scrapers_concurrently([], timeout=0)
