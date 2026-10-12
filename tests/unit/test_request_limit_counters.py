"""Counter protocol, cancellation and failure decisions."""

import asyncio

import pytest
from app.request_limits.limiter import LimiterUnavailable
from redis.exceptions import ConnectionError

from tests.unit.request_limit_fakes import limiter, redis_mock


async def test_rejection_and_private_key() -> None:
    value = limiter()
    redis_mock(value).eval.return_value = [0, 3]
    result = await value.check("private_read", "uid", "secret-user")
    assert not result.allowed and result.retry_after == 3
    args = redis_mock(value).eval.call_args.args
    assert args[1] == 1
    assert args[2].startswith("jobping:ratelimit:v1:")
    assert "secret-user" not in args[2]
    assert value.counts["private_read", "rejected"] == 1


async def test_shadow_observes_without_blocking() -> None:
    value = limiter("shadow")
    redis_mock(value).eval.return_value = [0, 2]
    assert (await value.check("public", "ip", "1.2.3.4")).allowed
    assert value.counts["public", "shadow_rejected"] == 1


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
async def test_redis_failure(mode: str) -> None:
    value = limiter(mode)
    redis_mock(value).eval.side_effect = ConnectionError("sensitive detail")
    if mode == "shadow":
        assert (await value.check("public", "ip", "x")).allowed
    else:
        with pytest.raises(LimiterUnavailable) as error:
            await value.check("public", "ip", "x")
        assert "sensitive" not in str(error.value)
    assert value.counts["public", "unavailable"] == 1


async def test_timeout_and_cancellation() -> None:
    value = limiter(timeout=0.01)
    redis_mock(value).eval.side_effect = lambda *args: None

    async def blocked(*args: object) -> None:
        await asyncio.Event().wait()

    redis_mock(value).eval.side_effect = blocked
    with pytest.raises(LimiterUnavailable):
        await value.check("ingress", "ip", "x")
    task = asyncio.create_task(value.check("public", "ip", "x"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
