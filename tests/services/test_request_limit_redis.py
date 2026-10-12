"""Run the actual Lua algorithms in a dedicated Redis DB with UUID key cleanup."""

import asyncio
from uuid import uuid4

import pytest
from app.request_limits.scripts import LEASE_ACQUIRE, LEASE_RELEASE, LEASE_RENEW, TOKEN_BUCKET
from redis.asyncio import Redis

pytestmark = pytest.mark.service_integration


async def test_atomic_counter_capacity_expiry_and_refill(test_redis: Redis) -> None:
    key = f"jobping_test:{uuid4().hex}:bucket"
    try:
        results = await asyncio.gather(
            *(test_redis.eval(TOKEN_BUCKET, 1, key, 0.5, 7) for _ in range(50))
        )
        assert sum(int(result[0]) for result in results) == 7
        assert all(int(result[1]) >= 1 for result in results if not result[0])
        assert 1 <= await test_redis.ttl(key) <= 14
        # Set a trustworthy older server timestamp to test refill without slow sleeps.
        seconds, microseconds = await test_redis.time()
        await test_redis.hset(key, mapping={"tokens": "0", "at": seconds + microseconds / 1e6 - 4})
        assert (await test_redis.eval(TOKEN_BUCKET, 1, key, 0.5, 7))[0] == 1
        await test_redis.delete(key)
        assert (await test_redis.eval(TOKEN_BUCKET, 1, key, 0.5, 7))[0] == 1
    finally:
        await test_redis.delete(key)


async def test_atomic_shared_connection_limits_and_crash_expiry(test_redis: Redis) -> None:
    prefix = f"jobping_test:{uuid4().hex}"
    ip, other, total = f"{prefix}:ip", f"{prefix}:other", f"{prefix}:total"
    try:
        results = await asyncio.gather(
            *(test_redis.eval(LEASE_ACQUIRE, 2, ip, total, str(i), 60, 3, 4) for i in range(30))
        )
        assert sum(results) == 3
        assert await test_redis.eval(LEASE_ACQUIRE, 2, other, total, "other", 60, 3, 4) == 1
        assert await test_redis.eval(LEASE_ACQUIRE, 2, other, total, "overflow", 60, 3, 4) == 0
        assert await test_redis.zcard(ip) == 3 and await test_redis.zcard(total) == 4
        member = (await test_redis.zrange(ip, 0, 0))[0]
        assert await test_redis.eval(LEASE_RENEW, 2, ip, total, member, 60) == 1
        await test_redis.eval(LEASE_RELEASE, 2, ip, total, member)
        assert await test_redis.eval(LEASE_RENEW, 2, ip, total, member, 60) == 0
        # Crash entries expire without an explicit release. Renewal cannot resurrect them.
        await test_redis.zadd(ip, {"crashed": 1})
        await test_redis.zadd(total, {"crashed": 1})
        assert await test_redis.eval(LEASE_RENEW, 2, ip, total, "crashed", 60) == 0
        assert await test_redis.eval(LEASE_ACQUIRE, 2, ip, total, "replacement", 60, 3, 4) == 1
        assert await test_redis.zscore(ip, "crashed") is None
    finally:
        await test_redis.delete(ip, other, total)
