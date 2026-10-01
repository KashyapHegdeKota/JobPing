"""Opt-in PostgreSQL/Redis fixtures refuse developer database configurations."""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def test_database_url() -> str:
    value = os.environ.get("JOBPING_TEST_DATABASE_URL")
    if not value:
        pytest.fail("service tests require JOBPING_TEST_DATABASE_URL")
    url = make_url(value)
    if url.host not in {"localhost", "127.0.0.1"} or not (url.database or "").startswith(
        "jobping_test"
    ):
        pytest.fail("service tests require a loopback database named jobping_test*")
    if url.drivername != "postgresql+psycopg":
        pytest.fail("service tests require postgresql+psycopg")
    return value


@pytest_asyncio.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    if os.environ.get("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 to run isolated PostgreSQL/Redis tests")
    url = test_database_url()
    schema = f"test_{uuid4().hex}"
    admin = create_async_engine(url)
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()


@pytest_asyncio.fixture
async def test_redis() -> AsyncIterator[Redis]:
    if os.environ.get("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 to run isolated PostgreSQL/Redis tests")
    value = os.environ.get("JOBPING_TEST_REDIS_URL")
    if not value:
        pytest.fail("service tests require JOBPING_TEST_REDIS_URL")
    url = make_url(value)
    if url.host not in {"localhost", "127.0.0.1"} or url.database != "15":
        pytest.fail("service tests require loopback Redis database 15")
    client = Redis.from_url(value, decode_responses=True)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()
