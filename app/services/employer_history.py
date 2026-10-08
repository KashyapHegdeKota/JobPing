"""Auditable imports of official employer records with reviewed entity mapping."""

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company
from app.schemas.job_details import EmployerRecord


class MappedEmployerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=255)
    record: EmployerRecord


class EmployerImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[MappedEmployerRecord] = Field(min_length=1, max_length=100_000)


def load_employer_history(path: Path) -> EmployerImport:
    if path.stat().st_size > 20_000_000:
        raise ValueError("Employer history file exceeds 20 MB")
    data = path.read_bytes()
    if len(data) > 20_000_000:
        raise ValueError("Employer history file exceeds 20 MB")
    return EmployerImport.model_validate_json(data)


def parse_official_csv(
    path: Path,
    *,
    provider: Literal["dol", "uscis"],
    year: int,
    source_url: str,
    aliases: dict[str, str],
) -> EmployerImport:
    """Read a government CSV export; aliases explicitly map legal entities to JobPing names.

    Unmapped employers are intentionally excluded; parent/subsidiary names are
    never joined by fuzzy matching. DOL counts cases, not positions or approvals.
    """
    if path.stat().st_size > 500_000_000:
        raise ValueError("CSV export exceeds 500 MB")
    counts: dict[str, int] = {}
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        for row in rows:
            values = {
                str(key).strip().upper().replace(" ", "_"): value
                for key, value in row.items()
                if key is not None
            }
            if provider == "dol":
                if values.get("VISA_CLASS") != "H-1B" or values.get("CASE_STATUS") != "Certified":
                    continue
                name = (values.get("EMPLOYER_NAME") or "").strip()
                case = values.get("CASE_NUMBER")
                if not case or case in seen:
                    continue
                seen.add(case)
                count = 1
            else:
                if int(values.get("FISCAL_YEAR", year)) != year:
                    continue
                name = (values.get("EMPLOYER_NAME") or "").strip()
                count = int((values.get("INITIAL_APPROVAL") or "0").replace(",", "")) + int(
                    (values.get("CONTINUING_APPROVAL") or "0").replace(",", "")
                )
            if name in aliases and count > 0:
                counts[name] = counts.get(name, 0) + count
    now = datetime.now(UTC)
    return EmployerImport(
        records=[
            MappedEmployerRecord(
                company=aliases[name],
                record=EmployerRecord(
                    kind="h1b_filings" if provider == "dol" else "h1b_approvals",
                    year=year,
                    count=count,
                    employer_name=name,
                    source_url=source_url,
                    observed_at=now,
                ),
            )
            for name, count in sorted(counts.items())
        ]
    )


async def import_employer_history(session: AsyncSession, data: EmployerImport) -> int:
    """Replace each dated dataset record idempotently; do not touch job identity/events."""
    names = {item.company for item in data.records}
    companies = {
        item.name: item
        for item in (
            await session.scalars(select(Company).where(Company.name.in_(names)).with_for_update())
        ).all()
    }
    if names - companies.keys():
        raise ValueError("History mapping contains companies absent from JobPing")
    for item in data.records:
        company = companies[item.company]
        records = [
            EmployerRecord.model_validate(record) for record in company.immigration_records or []
        ]
        key = (item.record.kind, item.record.year, item.record.employer_name)
        existing = next(
            (
                record
                for record in records
                if (record.kind, record.year, record.employer_name) == key
            ),
            None,
        )
        if existing is not None and existing.observed_at > item.record.observed_at:
            continue
        records = [
            record for record in records if (record.kind, record.year, record.employer_name) != key
        ]
        records.append(item.record)
        company.immigration_records = [
            record.model_dump(mode="json")
            for record in sorted(
                records, key=lambda record: (record.kind, record.year, record.employer_name)
            )
        ]
    await session.flush()
    return len(data.records)
