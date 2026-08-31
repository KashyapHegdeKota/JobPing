from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from app.db.repository import DatabaseRepository
from app.events.publisher import EventPublisher
from app.schemas.job import NormalizedJob
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ---------------------------------------------------------
# Windows Fix: Switch to SelectorEventLoop for psycopg
# ---------------------------------------------------------
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
# ---------------------------------------------------------


async def inject_test_job() -> None:
    # The actual AI-generated local development credentials
    db_url = "postgresql+psycopg://jobping:change-me-for-local-development@localhost:5432/jobping"

    engine = create_async_engine(db_url)
    session_maker = async_sessionmaker(engine)

    redis = Redis.from_url("redis://localhost:6379/0")
    publisher = EventPublisher(redis)

    # 64-character hex strings to satisfy Pydantic SHA-256 validation
    dummy_base_hash = "a" * 64
    dummy_content_hash = "b" * 64

    async with session_maker() as session:
        repo = DatabaseRepository(session, publisher)
        test_job = NormalizedJob(
            company_name="TestTech Inc.",
            title="Software Engineer Intern (Live Test)",
            apply_url="https://example.com/apply",
            location="Remote",
            season=2027,
            job_type="internship",
            is_closed=False,
            base_hash=dummy_base_hash,
            content_hash=dummy_content_hash,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        await repo.bulk_upsert_job_postings([test_job])
        await repo.wait_for_pending_events()
        print("✅ Test job injected and event published!")

    await engine.dispose()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(inject_test_job())
