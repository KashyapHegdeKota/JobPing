"""Low-cardinality operational counters; no client identifiers or request contents."""

import asyncio
import logging

from app.request_limits.limiter import RequestLimiter

logger = logging.getLogger(__name__)


async def report_limits(limiter: RequestLimiter) -> None:
    while True:
        await asyncio.sleep(60)
        logger.info(
            "Request protection mode=%s counters=%s",
            limiter.settings.mode,
            {
                f"{policy}.{outcome}": count
                for (policy, outcome), count in sorted(limiter.counts.items())
            },
        )
