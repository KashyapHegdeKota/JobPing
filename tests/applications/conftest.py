"""Isolated durable database and sanitized candidate fixtures."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.candidate.models import CandidateProfile
from app.db.models import Base
from app.schemas.job import NormalizedJob
from app.services.hasher import generate_base_hash, generate_content_hash
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def application_sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'applications.sqlite3'}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def application_job() -> NormalizedJob:
    base_hash = generate_base_hash("Fixture Co", "Software Engineer Intern")
    url = "https://boards.greenhouse.io/fixture/jobs/123"
    return NormalizedJob(
        company_name="Fixture Co",
        title="Software Engineer Intern",
        apply_url=url,
        location="Remote",
        location_source="greenhouse",
        season=2027,
        job_type="internship",
        base_hash=base_hash,
        content_hash=generate_content_hash(base_hash, url, "Remote", False),
    )


@pytest.fixture
def candidate_profile(tmp_path: Path) -> CandidateProfile:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% synthetic test resume\n%%EOF")
    return CandidateProfile.model_validate(
        {
            "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
            "education": {"school": "Fixture University", "degree": "BS", "major": "CS"},
            "links": {"github": "https://github.com/fixture"},
            "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
            "resume_path": str(resume),
        }
    )
