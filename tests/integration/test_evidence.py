"""Durability, stale-worker fencing, SQL parity and private account API boundaries."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from app.api.deps import get_db
from app.db.models import (
    EvidenceCheck,
    EvidenceDocument,
    JobOccurrence,
    JobPosting,
)
from app.db.repository import DatabaseRepository
from app.evidence.schemas import Document, Requirements
from app.evidence.screening import bucket_expression, evaluate
from app.evidence.store import claim, current_occurrence, enqueue, finish
from app.main import create_app
from app.notifications.security import Identity
from app.request_limits.dependencies import limited_identity
from app.schemas.job import NormalizedJob
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


async def seed(db: AsyncSession, job: NormalizedJob) -> tuple[JobPosting, JobOccurrence]:
    posting = await DatabaseRepository(db).save_job_posting(
        job.model_copy(update={"location": "Austin, TX, US"})
    )
    occurrence = await current_occurrence(db, posting.id)
    assert occurrence
    return posting, occurrence


async def complete(
    db: AsyncSession, job: JobPosting, occurrence: JobOccurrence, text: str, now: datetime = NOW
) -> EvidenceCheck:
    await enqueue(db, job, occurrence, now, refresh=True)
    selected = await claim(db, now)
    assert selected
    identifier, token, target = selected
    doc = Document(
        source_url=target["url"],
        final_url=target["url"],
        kind="posting",
        text=text,
        observed_at=now,
        role_identity=target,
        location=target["location"],
        source="greenhouse",
    )
    assert await finish(db, identifier, token, target, [doc], now)
    check = await db.get(EvidenceCheck, occurrence.id)
    assert check
    return check


async def test_coalescing_lease_recovery_and_timestamp_stability(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        await enqueue(db, posting, occurrence, NOW)
        await enqueue(db, posting, occurrence, NOW)
        assert await db.scalar(select(func.count()).select_from(EvidenceCheck)) == 1
        first = await claim(db, NOW)
        assert first and await claim(db, NOW) is None
        second = await claim(db, NOW + timedelta(seconds=91))
        assert second and second[1] != first[1]
        text = "We accept OPT. Base salary USD 80000-120000 per year."
        doc = Document(
            source_url=posting.apply_url,
            final_url=posting.apply_url,
            kind="posting",
            text=text,
            observed_at=NOW,
            role_identity=second[2],
            location=posting.location,
            source="greenhouse",
        )
        assert not await finish(
            db, first[0], first[1], first[2], [doc], NOW + timedelta(seconds=92)
        )
        assert await finish(db, *second, [doc], NOW + timedelta(seconds=92))
        await db.flush()
        original = posting.updated_at
        check = await complete(db, posting, occurrence, text, NOW + timedelta(hours=2))
        await db.flush()
        assert posting.updated_at == original
        assert check.verified_at == NOW + timedelta(hours=2)
        assert await db.scalar(select(func.count()).select_from(EvidenceDocument)) == 1


async def test_removed_statements_and_failed_refresh(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        check = await complete(db, posting, occurrence, "We accept OPT.")
        await enqueue(db, posting, occurrence, NOW + timedelta(hours=2), refresh=True)
        selected = await claim(db, NOW + timedelta(hours=2))
        assert selected and await finish(
            db, *selected, [], NOW + timedelta(hours=2), error="http_unavailable"
        )
        assert check.verified_at.replace(tzinfo=UTC) == NOW and check.status == "failed"
        assert check.projection["policies"]["opt"]["value"] == "allowed"
        check = await complete(
            db, posting, occurrence, "This role has no listed policy.", NOW + timedelta(hours=4)
        )
        assert check.projection["policies"] == {}
        assert await db.scalar(select(func.count()).select_from(EvidenceDocument)) == 2


@pytest.mark.parametrize(
    "text,status,age,expected",
    [
        ("Base salary USD 80000-120000 per year", "complete", 0, "matches"),
        ("Base salary USD 80000-90000 per year", "complete", 0, "excluded"),
        ("Base salary starting at USD 80000 per year", "complete", 0, "review"),
        ("Base salary USD 80000-120000 per year", "complete", 25, "review"),
        ("Base salary USD 80000-120000 per year", "failed", 0, "review"),
        ("Total compensation USD 100000-150000 per year", "complete", 0, "review"),
    ],
)
async def test_sql_python_bucket_parity(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    text: str,
    status: str,
    age: int,
    expected: str,
) -> None:
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(
            db, application_job.model_copy(update={"job_type": "new_grad"})
        )
        check = await complete(db, posting, occurrence, text)
        check.status = status
        check.verified_at = NOW - timedelta(hours=age)
        await db.flush()
        profile = Requirements(new_grad_annual=100000)
        python = evaluate(
            profile,
            check.projection,
            kind="new_grad",
            status=status,
            verified_at=check.verified_at,
            now=NOW,
        )
        sql = await db.scalar(
            select(bucket_expression(profile, dialect=db.get_bind().dialect.name, now=NOW))
            .select_from(JobPosting)
            .join(EvidenceCheck, EvidenceCheck.occurrence_id == occurrence.id)
        )
        assert python["bucket"] == sql == expected


async def test_target_change_rejects_late_completion(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        await enqueue(db, posting, occurrence, NOW)
        selected = await claim(db, NOW)
        assert selected
        posting.apply_url = posting.apply_url.replace("123", "456")
        assert not await finish(db, *selected, [], NOW)
        check = await db.get(EvidenceCheck, occurrence.id)
        assert check and check.error_code == "target_changed"


async def test_repost_starts_separate_evidence_and_rejects_previous_worker(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    from app.evidence.api import screening

    now = datetime.now(UTC)
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        old_check = await complete(db, posting, occurrence, "We accept OPT.", now)
        await enqueue(db, posting, occurrence, now + timedelta(hours=2), refresh=True)
        previous_claim = await claim(db, now + timedelta(hours=2))
        assert previous_claim
        repo = DatabaseRepository(db)
        await repo.save_job_posting(
            application_job.model_copy(update={"source": "greenhouse", "is_closed": True})
        )
        posting = await repo.save_job_posting(
            application_job.model_copy(
                update={
                    "source": "greenhouse",
                    "apply_url": str(application_job.apply_url).replace("123", "456"),
                    "location": "Austin, TX, US",
                }
            )
        )
        latest = await current_occurrence(db, posting.id)
        assert latest and latest.id != occurrence.id and latest.kind == "reposted"
        assert not await finish(db, *previous_claim, [], now + timedelta(hours=2, seconds=1))
        assert (
            screening(posting, None, Requirements(authorization="opt"), now)["bucket"] == "review"
        )
        new_check = await complete(
            db, posting, latest, "We do not accept OPT.", now + timedelta(hours=3)
        )
        assert new_check.projection["policies"]["opt"]["value"] == "denied"
        assert old_check.projection["policies"]["opt"]["value"] == "allowed"
        assert occurrence.details["policies"]["opt"]["value"] == "allowed"


@pytest.mark.parametrize(
    "text,requirements,location,title_changed,expected",
    [
        ("We accept OPT.", {"authorization": "opt"}, "Austin, TX, US", False, "matches"),
        ("We do not accept OPT.", {"authorization": "opt"}, "Austin, TX, US", False, "excluded"),
        (
            "We accept OPT. Candidates must be US citizens.",
            {"authorization": "opt"},
            "Austin, TX, US",
            False,
            "excluded",
        ),
        (
            "We accept OPT. Unless exempt, candidates must be US citizens.",
            {"authorization": "opt"},
            "Austin, TX, US",
            False,
            "review",
        ),
        (
            "We accept OPT. If required by a project, "
            "candidates must have permanent work authorization.",
            {"authorization": "opt"},
            "Austin, TX, US",
            False,
            "review",
        ),
        (
            "Visa sponsorship will be available in the future.",
            {"future_sponsorship": True},
            "Austin, TX, US",
            False,
            "matches",
        ),
        (
            "We accept OPT.",
            {"authorization": "opt", "locations": ["Seattle, WA, US"]},
            "Austin, TX, US",
            False,
            "review",
        ),
        ("We accept OPT.", {"authorization": "opt"}, "Toronto, Canada", False, "review"),
        ("We accept OPT.", {"authorization": "opt"}, "Austin, TX, US", True, "review"),
    ],
)
async def test_policy_location_and_target_bucket_parity(
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    text: str,
    requirements: dict,
    location: str,
    title_changed: bool,
    expected: str,
) -> None:
    from app.evidence.api import evidence_view, screening

    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(db, application_job)
        posting.location = location
        check = await complete(db, posting, occurrence, text, datetime.now(UTC))
        if title_changed:
            posting.title = "Different current title"
        await db.flush()
        now = datetime.now(UTC)
        profile = Requirements.model_validate(requirements)
        python = screening(posting, check, profile, now)
        sql = await db.scalar(
            select(bucket_expression(profile, dialect=db.get_bind().dialect.name, now=now))
            .select_from(JobPosting)
            .join(EvidenceCheck, EvidenceCheck.occurrence_id == occurrence.id)
        )
        assert python["bucket"] == sql == expected
        if title_changed:
            assert not (await evidence_view(db, posting, check))["fresh"]


async def test_profile_ownership_no_input_echo_and_paginated_private_feed(
    application_sessions: async_sessionmaker[AsyncSession], application_job: NormalizedJob
) -> None:
    app = create_app()
    current = Identity("alice", "alice@example.com", True)

    async def who() -> Identity:
        return current

    async def database() -> AsyncIterator[AsyncSession]:
        async with application_sessions() as db, db.begin():
            yield db

    app.dependency_overrides[limited_identity] = who
    app.dependency_overrides[get_db] = database
    async with application_sessions() as db, db.begin():
        posting, occurrence = await seed(
            db, application_job.model_copy(update={"job_type": "new_grad"})
        )
        await complete(
            db, posting, occurrence, "Base salary USD 80000-120000 per year", datetime.now(UTC)
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/v1/me/screening-profile", json={"new_grad_annual": 100000}
        )
        assert (
            response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        )
        response = await client.get("/api/v1/me/jobs?page_size=1")
        assert response.status_code == 200
        assert response.json()["counts"]["matches"] == 1
        assert response.json()["items"][0]["screening"]["bucket"] == "matches"
        private_value = "private-visa-fact-do-not-echo"
        response = await client.put(
            "/api/v1/me/screening-profile", json={"authorization": private_value}
        )
        assert response.status_code == 422 and private_value not in response.text
        current = Identity("bob", "bob@example.com", True)
        assert not (await client.get("/api/v1/me/screening-profile")).json()["configured"]
        current = Identity("alice", "alice@example.com", True)
        assert (await client.delete("/api/v1/me/screening-profile")).status_code == 200
        assert not (await client.get("/api/v1/me/screening-profile")).json()["configured"]
        current = Identity("bob", "bob@example.com", False)
        assert (await client.get("/api/v1/me/jobs")).status_code == 403


def test_evidence_migration_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'evidence.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert "evidence_checks" in inspect(engine).get_table_names()
        command.downgrade(config, "a7b83c96c247")
        assert "evidence_checks" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "screening_profiles" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
