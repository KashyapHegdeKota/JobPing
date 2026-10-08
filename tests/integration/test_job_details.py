"""Evidence persists without identity churn, including NO_OP and repost boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from app.api.v1.jobs import list_jobs
from app.db.models import Company, DiscoveryEvent, JobOccurrence
from app.db.repository import DatabaseRepository, PersistedJobResult
from app.schemas.job import NormalizedJob, RawJobPayload
from app.schemas.job_details import EmployerRecord
from app.services.employer_history import (
    EmployerImport,
    MappedEmployerRecord,
    import_employer_history,
)
from app.services.hasher import generate_base_hash, generate_content_hash
from app.services.job_details import extract_job_details
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def candidate(
    text: str = "",
    *,
    title: str = "Intern",
    identifier: int = 1,
    closed: bool = False,
    observed: datetime = NOW,
) -> NormalizedJob:
    url = f"https://job-boards.greenhouse.io/acme/jobs/{identifier}"
    base = generate_base_hash("Acme", title)
    raw = RawJobPayload(
        source="greenhouse",
        apply_url=url,
        location="Austin, TX, US",
        observed_at=observed,
        payload={"content": text},
    )
    return NormalizedJob(
        company_name="Acme",
        title=title,
        base_hash=base,
        content_hash=generate_content_hash(base, url, "Austin, TX, US", closed),
        apply_url=url,
        location="Austin, TX, US",
        season=2027,
        job_type="internship",
        is_closed=closed,
        observed_at=observed,
        source="greenhouse",
        source_id=f"greenhouse:acme:{identifier}",
        identity_namespace="greenhouse:acme",
        external_job_id=str(identifier),
        details=extract_job_details(raw),
    )


@pytest.mark.parametrize("bulk", [False, True])
async def test_enrichment_refresh_unknown_and_repost_boundary(
    application_sessions: async_sessionmaker[AsyncSession], bulk: bool
) -> None:
    async with application_sessions() as db:
        repo = DatabaseRepository(db)

        async def save(job: NormalizedJob) -> PersistedJobResult:
            return (
                (await repo.bulk_upsert_job_postings_with_outcomes([job]))[0]
                if bulk
                else await repo.save_job_posting_with_outcome(job)
            )

        initial = await save(candidate("We accept OPT. Base pay USD 40-50 per hour."))
        posting = initial.posting
        original_hash = posting.content_hash
        update = await save(
            candidate(
                "We do not accept OPT. Base pay USD 45-55 per hour.",
                observed=NOW + timedelta(days=1),
            )
        )
        assert update.state.value == "NO_OP"
        assert posting.content_hash == original_hash
        assert posting.details["policies"]["opt"]["value"] == "denied"
        assert posting.details["compensation"][0]["minimum"] == "45"
        await save(candidate())
        assert posting.details["policies"]["opt"]["value"] == "denied"
        await save(candidate(closed=True, observed=NOW + timedelta(days=2)))
        old_details = posting.details
        await save(candidate("We accept OPT.", identifier=2, observed=NOW + timedelta(days=3)))
        occurrences = list(
            (await db.scalars(select(JobOccurrence).order_by(JobOccurrence.id))).all()
        )
        assert len(occurrences) == 2
        assert occurrences[0].details == old_details
        assert posting.details["policies"]["opt"]["value"] == "allowed"
        assert posting.details["compensation"] == []
        assert await db.scalar(select(func.count()).select_from(DiscoveryEvent)) == 2


async def test_history_and_pay_filters_count_all_pages(
    application_sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with application_sessions() as db:
        repo = DatabaseRepository(db)
        await repo.bulk_upsert_job_postings(
            [
                candidate("We accept OPT. USD 40-50 per hour", title="A"),
                candidate("We do not accept OPT. USD 100000-150000 per year", title="B"),
                candidate(title="C"),
            ]
        )
        data = EmployerImport(
            records=[
                MappedEmployerRecord(
                    company="Acme",
                    record=EmployerRecord(
                        kind="h1b_filings",
                        year=2025,
                        count=2,
                        employer_name="ACME LLC",
                        source_url="https://www.dol.gov/data",
                        observed_at=NOW,
                    ),
                )
            ]
        )
        await import_employer_history(db, data)
        await import_employer_history(db, data)
        company = await db.scalar(select(Company))
        assert len(company.immigration_records) == 1
        approved = await list_jobs(db, policy="opt", h1b_history=True)
        assert approved.total == 1 and approved.items[0].title == "A"
        paid = await list_jobs(db, salary_reported=True, page_size=1)
        assert paid.total == 2 and paid.total_pages == 2
        hourly = await list_jobs(
            db, minimum_pay=Decimal(35), pay_currency="USD", pay_interval="hour"
        )
        assert hourly.total == 1 and hourly.items[0].title == "A"
        unknown = await list_jobs(db, policy="opt", policy_value="unknown")
        assert unknown.total == 1 and unknown.items[0].title == "C"
        assert len(approved.items[0].company.immigration_records) == 1


def test_details_migration_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'details.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0008_analytics")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert "details" in {row["name"] for row in inspect(engine).get_columns("job_postings")}
        command.downgrade(config, "0008_analytics")
        assert "details" not in {row["name"] for row in inspect(engine).get_columns("job_postings")}
        command.upgrade(config, "head")
        assert "immigration_records" in {
            row["name"] for row in inspect(engine).get_columns("companies")
        }
    finally:
        engine.dispose()
