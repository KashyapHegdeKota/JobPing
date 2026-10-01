"""Real PostgreSQL migration, application restart, and Redis Lua integration."""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.db.models import ApplicationAttempt, Base, DiscoveryEvent, JobPosting
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.pipelines.ats_pipeline import ATSPipeline
from app.schemas.job import JobType, NormalizedJob
from app.scrapers.greenhouse import GreenhouseScraper
from app.services.deduplicator import DeduplicationState, JobDeduplicator
from mcp.client.stdio import stdio_client
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from mcp import ClientSession, StdioServerParameters

pytestmark = pytest.mark.service_integration


async def test_mocked_greenhouse_real_redis_postgres_and_mcp_ready(
    postgres_engine: AsyncEngine,
    test_redis: Redis,
    tmp_path: Path,
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        schema = await connection.scalar(text("SHOW search_path"))
    namespace = f"jobping:test:{uuid4().hex}"
    deduplicator = JobDeduplicator(test_redis, key_namespace=namespace)
    discovered_hash: str | None = None
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 123,
                        "title": "Software Engineer Intern",
                        "absolute_url": "https://job-boards.greenhouse.io/fixture/jobs/123",
                        "location": {"name": "Remote"},
                        "content": "Synthetic fixture role",
                    }
                ]
            },
        )
    )
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            async with GreenhouseScraper(client=client, company="fixture") as scraper:
                async with sessions() as session:
                    result = await ATSPipeline(
                        [scraper], deduplicator, session, season=2027, job_type=JobType.INTERNSHIP
                    ).run()
                    assert len(result.outcomes) == 1 and not result.failures and not result.rejected
                    discovered_hash = result.outcomes[0].job.base_hash
                    repeat = await ATSPipeline(
                        [scraper], deduplicator, session, season=2027, job_type=JobType.INTERNSHIP
                    ).run()
                    assert repeat.outcomes[0].state == DeduplicationState.NO_OP
                    await session.commit()
        scoped_url = postgres_engine.url.set(
            query={"options": f"-csearch_path={schema}"}
        ).render_as_string(hide_password=False)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp.server"],
            cwd=str(tmp_path),
            env={
                **os.environ,
                "DATABASE_URL": scoped_url,
                "PYTHONPATH": str(Path.cwd()),
                "JOBPING_CANDIDATE_PATH": "",
                "JOBPING_ANSWERS_PATH": "",
                "JOBPING_STORIES_PATH": "",
                "JOBPING_RULES_PATH": "",
            },
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as mcp:
                await mcp.initialize()
                response = await mcp.call_tool("jobs_get_next", {})
                assert not response.is_error
                queued = json.loads(response.content[0].text)
                response = await mcp.call_tool(
                    "application_start",
                    {"job_id": queued["job_id"], "current_url": queued["apply_url"]},
                )
                assert not response.is_error
                response = await mcp.call_tool(
                    "application_mark_ready", {"job_id": queued["job_id"]}
                )
                assert not response.is_error
                ready = json.loads(response.content[0].text)
                assert ready["status"] == "ready_to_submit" and ready["submitted_at"] is None
        async with sessions() as fresh:
            for model in (JobPosting, ApplicationAttempt, DiscoveryEvent):
                assert await fresh.scalar(select(func.count()).select_from(model)) == 1
    finally:
        if discovered_hash is not None:
            await test_redis.delete(f"{namespace}:{discovered_hash}")
        await deduplicator.aclose()


async def test_postgres_discovery_to_ready_survives_restart(
    postgres_engine: AsyncEngine,
    application_job: NormalizedJob,
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        job_id = posting.id
        server = JobPingMCPServer(repository)
        assert (await server.jobs_get_next())["job_id"] == job_id
        await server.application_start(job_id, posting.apply_url)
        await server.application_save_answer(
            job_id, question="Why?", answer="Reviewed", source="human"
        )
        await server.application_update_checkpoint(job_id, posting.apply_url, "review")
        await server.application_mark_ready(job_id, posting.apply_url)
        await session.commit()
    async with sessions() as fresh:
        server = JobPingMCPServer(DatabaseRepository(fresh))
        checkpoint = await server.application_get_checkpoint(job_id)
        assert checkpoint["status"] == "ready_to_submit" and checkpoint["submitted_at"] is None
        assert checkpoint["attempt_count"] == 1
        assert datetime.fromisoformat(checkpoint["started_at"]).utcoffset() is not None
        assert await fresh.scalar(select(func.count()).select_from(ApplicationAttempt)) == 1
        assert await fresh.scalar(select(func.count()).select_from(DiscoveryEvent)) == 1


async def test_postgres_migrations_round_trip_isolated_schema(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.scalar(text("SHOW search_path"))
    url = make_url(postgres_engine.url).set(query={"options": f"-csearch_path={schema}"})
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "head")
    async with postgres_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM alembic_version")) == 1
        assert await connection.scalar(select(func.count()).select_from(JobPosting)) == 0
    await asyncio.to_thread(command.downgrade, config, "base")
    async with postgres_engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('application_attempts')")) is None
        assert await connection.scalar(text("SELECT to_regtype('job_type')")) is None
    await asyncio.to_thread(command.upgrade, config, "head")


async def test_redis_real_lua_concurrency_and_ttl_refresh(test_redis: Redis) -> None:
    namespace = f"jobping:test:{uuid4().hex}"
    deduplicator = JobDeduplicator(test_redis, key_namespace=namespace, ttl_seconds=120)
    key = f"{namespace}:{'a' * 64}"
    try:
        results = await asyncio.gather(
            *[
                deduplicator.classify_and_update(
                    base_hash="a" * 64, content_hash="b" * 64, is_closed=False
                )
                for _ in range(12)
            ]
        )
        assert results.count(DeduplicationState.NEW_ROLE) == 1
        assert results.count(DeduplicationState.NO_OP) == 11
        await test_redis.expire(key, 1)
        assert (
            await deduplicator.classify_and_update(
                base_hash="a" * 64, content_hash="b" * 64, is_closed=False
            )
            == DeduplicationState.NO_OP
        )
        assert await test_redis.ttl(key) > 100
        assert (
            await deduplicator.classify_and_update(
                base_hash="a" * 64, content_hash="c" * 64, is_closed=False
            )
            == DeduplicationState.ROLE_UPDATED
        )
        assert (
            await deduplicator.classify_and_update(
                base_hash="a" * 64, content_hash="d" * 64, is_closed=True
            )
            == DeduplicationState.ROLE_CLOSED
        )
    finally:
        await test_redis.delete(key)
        await deduplicator.aclose()
    assert await test_redis.ping()  # Injected resource remains caller-owned.
