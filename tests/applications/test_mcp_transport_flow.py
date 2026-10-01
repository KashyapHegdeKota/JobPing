"""Exercise discovery through ready and pause/resume over the official stdio transport."""

import json
import os
import sys
from pathlib import Path

from app.db.models import Base
from app.db.repository import DatabaseRepository
from app.schemas.job import NormalizedJob
from mcp.client.stdio import stdio_client
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mcp import ClientSession, StdioServerParameters


async def payload(
    client: ClientSession, tool: str, arguments: dict[str, object]
) -> dict[str, object]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def test_stdio_mcp_queue_pause_resume_ready_and_restart(
    tmp_path: Path,
    application_job: NormalizedJob,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'transport.sqlite3'}"
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            posting = await DatabaseRepository(session).save_job_posting(application_job)
            job_id = posting.id
            await session.commit()
    finally:
        await engine.dispose()
    # A fresh working directory prevents default private/ candidate configuration leakage.
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.server"],
        cwd=str(tmp_path),
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "PYTHONPATH": str(Path.cwd()),
            "JOBPING_CANDIDATE_PATH": "",
            "JOBPING_ANSWERS_PATH": "",
            "JOBPING_STORIES_PATH": "",
            "JOBPING_RULES_PATH": "",
        },
    )
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            queued = await payload(client, "jobs_get_next", {})
            assert queued["job_id"] == job_id and queued["status"] == "queued"
            invalid = await client.call_tool("application_mark_submitted", {"job_id": job_id})
            assert invalid.is_error
            await payload(
                client,
                "application_start",
                {"job_id": job_id, "current_url": str(application_job.apply_url)},
            )
            await payload(
                client,
                "application_mark_verification_required",
                {
                    "job_id": job_id,
                    "verification_type": "captcha",
                    "current_url": str(application_job.apply_url),
                    "instruction": "Complete challenge in browser.",
                },
            )
            paused = await payload(client, "application_get_checkpoint", {"job_id": job_id})
            assert paused["verification_type"] == "captcha"
            await payload(client, "application_mark_verification_complete", {"job_id": job_id})
            ready = await payload(client, "application_mark_ready", {"job_id": job_id})
            assert ready["status"] == "ready_to_submit" and ready["submitted_at"] is None
            invalid_stage = await client.call_tool(
                "application_update_checkpoint", {"job_id": job_id, "stage": "invented"}
            )
            assert invalid_stage.is_error
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            checkpoint = await payload(client, "application_get_checkpoint", {"job_id": job_id})
            assert checkpoint["status"] == "ready_to_submit" and checkpoint["attempt_count"] == 1
            assert checkpoint["submitted_at"] is None
