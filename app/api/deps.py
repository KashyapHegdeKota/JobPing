"""Request-scoped dependencies for shared database and Redis resources."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _unavailable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one transactional session and close it after the request.

    Successful routes commit when the transaction context exits. Exceptions,
    including cancellation, propagate through the context so SQLAlchemy rolls
    the transaction back before closing the request-scoped session.
    """
    sessionmaker: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "sessionmaker", None
    )
    if sessionmaker is None or not callable(sessionmaker):
        raise _unavailable(
            "Database session factory is unavailable; application startup is incomplete"
        )

    async with sessionmaker() as session:
        async with session.begin():
            yield session


async def get_redis(request: Request) -> Redis:
    """Return the lifespan-owned Redis client without transferring ownership."""
    client: Redis | None = getattr(request.app.state, "redis", None)
    if client is None:
        raise _unavailable("Redis client is unavailable; application startup is incomplete")
    return client


__all__ = ["get_db", "get_redis"]
