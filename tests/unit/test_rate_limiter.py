"""Deterministic tests for rate limiting and user-agent rotation."""

from __future__ import annotations

import asyncio
import random

import pytest
from app.utils.rate_limiter import DomainRateLimiter, RateLimit, UserAgentRotator


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_capacity_is_immediate_then_tokens_refill() -> None:
    time = FakeTime()
    limiter = DomainRateLimiter(RateLimit(2, 2), clock=time.clock, sleep=time.sleep)

    await limiter.acquire("https://Example.COM/jobs")
    await limiter.acquire("example.com")
    assert time.delays == []
    await limiter.acquire("EXAMPLE.com:443")
    assert time.delays == [pytest.approx(0.5)]


@pytest.mark.asyncio
async def test_domains_have_independent_buckets_and_overrides() -> None:
    time = FakeTime()
    limiter = DomainRateLimiter(
        RateLimit(1, 1),
        domains={"fast.test": RateLimit(10, 1)},
        clock=time.clock,
        sleep=time.sleep,
    )
    await limiter.acquire("slow.test")
    await limiter.acquire("fast.test")
    await limiter.acquire("fast.test")
    assert time.delays == [pytest.approx(0.1)]


@pytest.mark.asyncio
async def test_concurrent_waiters_complete_without_lock_held_during_sleep() -> None:
    time = FakeTime()
    limiter = DomainRateLimiter(RateLimit(1, 1), clock=time.clock, sleep=time.sleep)
    await limiter.acquire("jobs.test")
    await asyncio.gather(limiter.acquire("jobs.test"), limiter.acquire("other.test"))
    assert time.delays == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_cancellation_propagates_and_limiter_remains_usable() -> None:
    sleeping = asyncio.Event()

    async def blocked_sleep(delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    limiter = DomainRateLimiter(RateLimit(1, 1), sleep=blocked_sleep)
    await limiter.acquire("jobs.test")
    task = asyncio.create_task(limiter.acquire("jobs.test"))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await limiter.acquire("other.test")


@pytest.mark.parametrize("value", ["", "http:///missing", "   "])
def test_invalid_domains_are_rejected(value: str) -> None:
    limiter = DomainRateLimiter(RateLimit(1, 1))
    with pytest.raises(ValueError):
        asyncio.run(limiter.acquire(value))


def test_provider_rotation_and_header_merge_do_not_mutate_or_overwrite() -> None:
    values = iter(("Agent One", "Agent Two"))
    rotator = UserAgentRotator(provider=lambda: next(values))
    original = {"Accept": "application/json"}
    first = rotator.headers(original)
    second = rotator.headers({"user-agent": "Caller"})

    assert first == {"Accept": "application/json", "User-Agent": "Agent One"}
    assert second == {"user-agent": "Caller"}
    assert original == {"Accept": "application/json"}


def test_provider_failure_uses_deterministic_offline_fallback() -> None:
    def fail() -> str:
        raise OSError("offline")

    rotator = UserAgentRotator(provider=fail, rng=random.Random(0), fallback=("A", "B"))
    assert rotator.get() == "B"


def test_empty_provider_value_uses_fallback() -> None:
    rotator = UserAgentRotator(provider=lambda: "  ", fallback=("Static",))
    assert rotator.get() == "Static"


def test_configuration_validation() -> None:
    with pytest.raises(ValueError):
        RateLimit(0, 1)
    with pytest.raises(ValueError):
        DomainRateLimiter(RateLimit(1, 1), max_domains=0)
    with pytest.raises(ValueError):
        UserAgentRotator(fallback=())
