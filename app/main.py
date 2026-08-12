"""FastAPI application factory and owned service lifecycle."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_URL = (
    "postgresql+psycopg://jobping:change-me-for-local-development@localhost:5432/jobping"
)
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_CORS_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")

OPENAPI_TAGS = [
    {"name": "jobs", "description": "Search and filter normalized job postings."},
    {"name": "companies", "description": "Browse companies with discovered roles."},
    {"name": "stats", "description": "Inspect ingestion and listing statistics."},
    {"name": "live", "description": "Subscribe to real-time job events."},
]

EngineFactory = Callable[[str], AsyncEngine]
RedisFactory = Callable[[str], Redis]


def _as_async_database_url(url: str) -> str:
    """Normalize supported PostgreSQL URLs for SQLAlchemy's async engine."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql+psycopg2://"):
        return url.replace("postgresql+psycopg2://", "postgresql+psycopg://", 1)
    return url


def _cors_origins(value: str | None) -> list[str]:
    if value is None:
        return list(DEFAULT_CORS_ORIGINS)
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def _environment_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _default_engine_factory(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def _default_redis_factory(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True)


def create_app(
    *,
    database_url: str | None = None,
    redis_url: str | None = None,
    cors_origins: list[str] | None = None,
    startup_health_checks: bool | None = None,
    engine_factory: EngineFactory = _default_engine_factory,
    redis_factory: RedisFactory = _default_redis_factory,
) -> FastAPI:
    """Build an application whose lifespan owns its database and Redis clients."""
    resolved_database_url = _as_async_database_url(
        database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
    )
    resolved_redis_url = redis_url or os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
    allowed_origins = (
        cors_origins if cors_origins is not None else _cors_origins(os.environ.get("CORS_ORIGINS"))
    )
    check_services = (
        startup_health_checks
        if startup_health_checks is not None
        else _environment_flag("STARTUP_HEALTH_CHECKS", default=True)
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = engine_factory(resolved_database_url)
        redis = redis_factory(resolved_redis_url)
        application.state.db_engine = engine
        application.state.db_sessionmaker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        application.state.redis = redis
        try:
            if check_services:
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
                await redis.ping()
            yield
        finally:
            await redis.aclose()
            await engine.dispose()

    application = FastAPI(
        title="JobPing API",
        summary="Low-latency internship and new-grad job discovery API",
        version="0.2.0",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials="*" not in allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return application


app = create_app()

__all__ = ["app", "create_app"]
