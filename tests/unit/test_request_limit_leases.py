"""Lease refusal, expiry renewal and cleanup under provider failures."""

import asyncio
from unittest.mock import patch

import pytest
from app.request_limits.leases import Lease, StreamLeases
from redis.exceptions import ConnectionError

from tests.unit.request_limit_fakes import limiter, redis_mock


async def test_capacity_rejection_and_shadow_reservation() -> None:
    value = limiter()
    redis_mock(value).eval.return_value = 0
    assert await StreamLeases(value).acquire("x") is None
    value = limiter("shadow")
    redis_mock(value).eval.side_effect = [0, 1]
    lease = await StreamLeases(value).acquire("x")
    assert lease is not None
    assert redis_mock(value).eval.call_args.args[-2:] == (0, 0)


async def test_renewal_loss_is_not_reacquired() -> None:
    value = limiter()
    redis_mock(value).eval.return_value = 0
    with patch("app.request_limits.leases.asyncio.sleep", return_value=None):
        await StreamLeases(value).renew_until_lost(Lease(["ip", "global"], "token"))
    assert redis_mock(value).eval.call_count == 1


async def test_repeated_renewal_failure_stops_before_expiry() -> None:
    value = limiter()
    redis_mock(value).eval.side_effect = ConnectionError()
    with patch("app.request_limits.leases.asyncio.sleep", return_value=None):
        await StreamLeases(value).renew_until_lost(Lease(["ip", "global"], "token"))
    assert redis_mock(value).eval.call_count == 2
    await StreamLeases(value).release(Lease(["ip", "global"], "token"))
    assert value.counts["connect", "release_unavailable"] == 1


async def test_renewal_cancellation_propagates() -> None:
    task = asyncio.create_task(StreamLeases(limiter()).renew_until_lost(Lease([], "token")))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
