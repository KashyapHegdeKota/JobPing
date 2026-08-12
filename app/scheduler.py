"""AsyncIO polling scheduler for independently configured job sources."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

type PollCallable = Callable[[], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class PollTarget:
    """One uniquely named polling callback and its interval."""

    name: str
    interval_seconds: float
    callback: PollCallable

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("target name must not be empty")
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")


class SchedulerDaemon:
    """Register and operate non-overlapping UTC interval poll jobs."""

    def __init__(
        self,
        *,
        scheduler: AsyncIOScheduler | None = None,
        logger: logging.Logger | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.scheduler = scheduler or AsyncIOScheduler(timezone=UTC)
        self._logger = logger or logging.getLogger(__name__)
        self._now = now or (lambda: datetime.now(UTC))
        self._locks: dict[str, asyncio.Lock] = {}
        self._started = False

    def register(self, target: PollTarget) -> None:
        """Add or replace one polling target using APScheduler singleton controls."""
        lock = self._locks.setdefault(target.name, asyncio.Lock())

        async def execute() -> None:
            if lock.locked():
                self._logger.warning("scheduler.poll.skipped", extra={"target": target.name})
                return
            async with lock:
                try:
                    await target.callback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.exception("scheduler.poll.failed", extra={"target": target.name})

        self.scheduler.add_job(
            execute,
            trigger=IntervalTrigger(seconds=target.interval_seconds, timezone=UTC),
            id=f"poll:{target.name}",
            name=target.name,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(1, int(target.interval_seconds)),
            next_run_time=self._now(),
        )

    def start(self) -> None:
        """Start APScheduler once on the active asyncio loop."""
        if not self._started:
            self.scheduler.start()
            self._started = True

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop APScheduler idempotently."""
        if self._started:
            self.scheduler.shutdown(wait=wait)
            self._started = False

    async def serve(self, stop_event: asyncio.Event | None = None) -> None:
        """Run until signalled or cancelled, always shutting down safely."""
        event = stop_event or asyncio.Event()
        self.start()
        try:
            await event.wait()
        finally:
            self.shutdown(wait=False)


def parse_intervals(values: list[str]) -> Mapping[str, float]:
    """Parse repeatable ``DOMAIN=SECONDS`` CLI values."""
    result: dict[str, float] = {}
    for value in values:
        name, separator, raw_seconds = value.partition("=")
        if not separator or not name.strip():
            raise ValueError(f"invalid interval {value!r}; expected DOMAIN=SECONDS")
        try:
            seconds = float(raw_seconds)
        except ValueError as exc:
            raise ValueError(f"invalid interval seconds in {value!r}") from exc
        if seconds <= 0:
            raise ValueError("interval seconds must be positive")
        result[name.strip()] = seconds
    return result


__all__ = ["PollCallable", "PollTarget", "SchedulerDaemon", "parse_intervals"]
