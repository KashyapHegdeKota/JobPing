"""Verify identity once and consume a UID budget before opening private SQL sessions."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.notifications.security import Identity, identity
from app.request_limits.errors import rejection
from app.request_limits.limiter import LimiterUnavailable, RequestLimiter
from app.request_limits.policies import family


async def limited_identity(
    request: Request, who: Annotated[Identity, Depends(identity)]
) -> Identity:
    limiter: RequestLimiter | None = getattr(request.app.state, "request_limiter", None)
    if limiter is not None and limiter.settings.mode != "off":
        try:
            decision = await limiter.check(family(request.url.path, request.method), "uid", who.uid)
        except LimiterUnavailable:
            raise rejection(503) from None
        if not decision.allowed:
            raise rejection(429, decision.retry_after)
    return who


async def private_db(
    who: Annotated[Identity, Depends(limited_identity)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AsyncIterator[AsyncSession]:
    # FastAPI evaluates these dependencies in order and caches limited_identity.
    yield db
