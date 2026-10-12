"""Crash-safe connection leases shared across SSE and WebSocket protocols."""

import asyncio
import uuid
from dataclasses import dataclass

from app.request_limits.identity import opaque_key
from app.request_limits.limiter import LimiterUnavailable, RequestLimiter
from app.request_limits.scripts import LEASE_ACQUIRE, LEASE_RELEASE, LEASE_RENEW


@dataclass(frozen=True)
class Lease:
    keys: list[str]
    token: str


class StreamLeases:
    def __init__(self, limiter: RequestLimiter) -> None:
        self.limiter = limiter

    async def acquire(self, address: str) -> Lease | None:
        settings = self.limiter.settings
        keys = [
            opaque_key(settings.secret, "streams", address),
            "jobping:ratelimit:v1:streams:global",
        ]
        lease = Lease(keys, uuid.uuid4().hex)
        result = await self.limiter.evaluate(
            LEASE_ACQUIRE,
            keys,
            lease.token,
            settings.lease_seconds,
            settings.streams_per_ip,
            settings.streams_total,
        )
        if not result:
            self.limiter.counts["connect", "capacity_rejected"] += 1
            if settings.mode != "shadow":
                return None
            await self.limiter.evaluate(
                LEASE_ACQUIRE, keys, lease.token, settings.lease_seconds, 0, 0
            )
        return lease

    async def renew_until_lost(self, lease: Lease) -> None:
        settings = self.limiter.settings
        failures = 0
        while True:
            await asyncio.sleep(settings.lease_seconds / 3)
            try:
                renewed = await self.limiter.evaluate(
                    LEASE_RENEW, lease.keys, lease.token, settings.lease_seconds
                )
                failures = 0
                if not renewed:
                    return
            except LimiterUnavailable:
                failures += 1
                self.limiter.counts["connect", "renewal_unavailable"] += 1
                # Close before the last trustworthy lease can expire.
                if failures >= 2:
                    return

    async def release(self, lease: Lease) -> None:
        try:
            await self.limiter.evaluate(LEASE_RELEASE, lease.keys, lease.token)
        except LimiterUnavailable:
            # Expiry bounds stale occupancy after failures or process crashes.
            self.limiter.counts["connect", "release_unavailable"] += 1
