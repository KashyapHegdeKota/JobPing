"""Tests for scheduler registration, lifecycle, and CLI configuration."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from app.cli import app
from app.scheduler import PollTarget, SchedulerDaemon, parse_intervals
from click import unstyle
from typer.testing import CliRunner


def test_registration_uses_utc_interval_and_singleton_options() -> None:
    scheduler = Mock()
    daemon = SchedulerDaemon(
        scheduler=scheduler,
        now=lambda: datetime(2026, 8, 12, tzinfo=UTC),
    )

    async def poll() -> None:
        pass

    daemon.register(PollTarget("github.com", 30, poll))
    kwargs = scheduler.add_job.call_args.kwargs
    assert kwargs["id"] == "poll:github.com"
    assert kwargs["replace_existing"] is True
    assert kwargs["max_instances"] == 1
    assert kwargs["coalesce"] is True
    assert kwargs["next_run_time"].tzinfo is UTC
    assert kwargs["trigger"].interval.total_seconds() == 30


@pytest.mark.parametrize("seconds", [0, -1])
def test_interval_validation(seconds: float) -> None:
    async def poll() -> None:
        pass

    with pytest.raises(ValueError):
        PollTarget("target", seconds, poll)
    with pytest.raises(ValueError):
        parse_intervals([f"target={seconds}"])


def test_start_and_shutdown_are_idempotent() -> None:
    scheduler = Mock()
    daemon = SchedulerDaemon(scheduler=scheduler)
    daemon.start()
    daemon.start()
    daemon.shutdown(wait=False)
    daemon.shutdown(wait=False)
    scheduler.start.assert_called_once()
    scheduler.shutdown.assert_called_once_with(wait=False)


async def test_target_does_not_overlap() -> None:
    scheduler = Mock()
    daemon = SchedulerDaemon(scheduler=scheduler)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def poll() -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    daemon.register(PollTarget("slow", 1, poll))
    execute = scheduler.add_job.call_args.args[0]
    first = asyncio.create_task(execute())
    await entered.wait()
    await execute()
    release.set()
    await first
    assert calls == 1


async def test_job_error_is_logged_and_nonfatal(caplog: pytest.LogCaptureFixture) -> None:
    scheduler = Mock()
    daemon = SchedulerDaemon(scheduler=scheduler)

    async def fail() -> None:
        raise RuntimeError("offline")

    daemon.register(PollTarget("broken", 1, fail))
    with caplog.at_level(logging.ERROR):
        await scheduler.add_job.call_args.args[0]()
    assert "scheduler.poll.failed" in caplog.messages


def test_cli_help_and_dry_run_do_not_start_daemon() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["start-scheduler", "--help"])
    assert help_result.exit_code == 0
    assert "--dry-run" in unstyle(help_result.output)
    result = runner.invoke(
        app,
        ["start-scheduler", "--interval", "github.com=15", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "github.com=15s" in result.output
