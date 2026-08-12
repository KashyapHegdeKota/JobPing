"""Tests for in-process scraper health diagnostics."""

from __future__ import annotations

import asyncio

import pytest
from app.utils.metrics import ScraperMetrics


@pytest.mark.asyncio
async def test_records_scraper_aggregates_and_errors() -> None:
    metrics = ScraperMetrics()
    await metrics.record_run("Greenhouse", duration_seconds=2.0, jobs_parsed=4)
    await metrics.record_run(
        "greenhouse", duration_seconds=4.0, jobs_parsed=0, error=RuntimeError("down")
    )

    health = (await metrics.scraper_health())[0]
    assert health.runs == 2
    assert health.successes == health.failures == 1
    assert health.jobs_parsed == 4
    assert health.average_duration_seconds == 3.0
    assert health.error_rate == 0.5
    assert health.last_error == "RuntimeError: down"
    assert len(await metrics.recent_errors()) == 1


@pytest.mark.asyncio
async def test_proxy_metrics_redact_credentials() -> None:
    metrics = ScraperMetrics()
    proxy = "http://secret-user:secret-pass@proxy.test:8080"
    await metrics.record_proxy(proxy, success=False, status=429)
    await metrics.record_proxy(proxy, success=True)

    health = (await metrics.proxy_health())[0]
    assert health.proxy == "http://proxy.test:8080"
    assert "secret" not in repr(health)
    assert health.failure_rate == 0.5


@pytest.mark.asyncio
async def test_concurrent_updates_are_not_lost() -> None:
    metrics = ScraperMetrics()
    await asyncio.gather(
        *(metrics.record_run("lever", duration_seconds=0.1, jobs_parsed=1) for _ in range(100))
    )
    health = (await metrics.scraper_health())[0]
    assert health.runs == health.successes == health.jobs_parsed == 100


@pytest.mark.asyncio
async def test_timer_records_success_and_failure() -> None:
    metrics = ScraperMetrics()
    async with metrics.timer("workday") as timer:
        timer.jobs_parsed = 3
    with pytest.raises(ValueError):
        async with metrics.timer("workday"):
            raise ValueError("bad page")
    health = (await metrics.scraper_health())[0]
    assert health.runs == 2
    assert health.jobs_parsed == 3
    assert health.failures == 1


@pytest.mark.asyncio
async def test_error_history_is_bounded_and_inputs_validated() -> None:
    metrics = ScraperMetrics(recent_errors_limit=2)
    for index in range(3):
        await metrics.record_run(
            "custom", duration_seconds=0, jobs_parsed=0, error=RuntimeError(str(index))
        )
    assert [item[2] for item in await metrics.recent_errors()] == [
        "RuntimeError: 1",
        "RuntimeError: 2",
    ]
    with pytest.raises(ValueError):
        await metrics.record_run(" ", duration_seconds=0, jobs_parsed=0)
    with pytest.raises(ValueError):
        await metrics.record_run("x", duration_seconds=-1, jobs_parsed=0)
