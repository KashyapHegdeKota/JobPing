"""Bounded read-only deployment probes; no dependency error details are returned."""

import asyncio
import os

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

router = APIRouter()


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/version", include_in_schema=False)
async def version() -> dict[str, str]:
    return {
        "commit": os.environ.get("APP_GIT_SHA", "unknown"),
        "color": os.environ.get("APP_COLOR", "local"),
    }


@router.get("/ready", include_in_schema=False)
async def ready(request: Request) -> dict[str, str]:
    state = request.app.state
    consumer = getattr(state, "redis_event_consumer", None)
    if not consumer or not consumer.is_running:
        raise HTTPException(503, "Application is not ready")
    try:
        async with asyncio.timeout(3):
            async with state.db_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            await state.redis.ping()
            limiter = getattr(state, "request_limiter", None)
            if limiter is not None:
                await limiter.redis.ping()
    except Exception:
        raise HTTPException(503, "Application is not ready") from None
    return {"status": "ready"}
