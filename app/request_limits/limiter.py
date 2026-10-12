"""Shared counters with deadlines, injectable Redis and bounded operational metrics."""

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.request_limits.identity import opaque_key
from app.request_limits.scripts import TOKEN_BUCKET
from app.request_limits.settings import LimitSettings


class LimiterUnavailable(Exception):
    """No trustworthy shared admission decision could be made."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after: int = 0


class RequestLimiter:
    def __init__(self, redis: Redis, settings: LimitSettings) -> None:
        self.redis = redis
        self.settings = settings
        self.counts: Counter[tuple[str, str]] = Counter()

    async def evaluate(self, script: str, keys: list[str], *args: object) -> list[int] | int:
        try:
            async with asyncio.timeout(self.settings.timeout):
                return cast(list[int] | int, await self.redis.eval(script, len(keys), *keys, *args))
        except (RedisError, TimeoutError):
            raise LimiterUnavailable from None

    async def check(self, policy: str, kind: str, subject: str) -> Decision:
        value = self.settings.policies[policy]
        key = opaque_key(self.settings.secret, f"{kind}:{policy}", subject)
        try:
            allowed, retry = cast(
                list[int], await self.evaluate(TOKEN_BUCKET, [key], value.rate, value.burst)
            )
        except LimiterUnavailable:
            self.counts[policy, "unavailable"] += 1
            if self.settings.mode == "shadow":
                return Decision(True)
            raise
        result = Decision(bool(allowed), max(1, int(retry)) if not allowed else 0)
        outcome = "allowed" if result.allowed else "rejected"
        if not result.allowed and self.settings.mode == "shadow":
            outcome = "shadow_rejected"
        self.counts[policy, outcome] += 1
        return Decision(True) if self.settings.mode == "shadow" else result
