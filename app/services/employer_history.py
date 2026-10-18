"""Auditable imports of official employer records with reviewed entity mapping."""

import csv
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company
from app.schemas.job_details import EmployerRecord

_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "lp",
        "llp",
        "plc",
        "pllc",
        "pc",
    }
)
_FY_RE = re.compile(r"(?:^|[^A-Za-z0-9])FY\s*(\d{2,4})(?!\d)", re.IGNORECASE)
DEFAULT_DOL_SOURCE_URL = "https://www.dol.gov/agencies/eta/foreign-labor/performance"


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


def infer_fiscal_year(path: Path) -> int | None:
    """Extract a 4-digit fiscal year from an official DOL/USCIS disclosure filename."""
    match = _FY_RE.search(path.stem)
    if not match:
        return None
    raw = int(match.group(1))
    year = 2000 + raw if raw < 100 else raw
    return year if 2000 <= year <= 2100 else None


def canonical_company_key(name: str) -> str:
    """Normalize a company or legal entity name by stripping only standard legal suffixes.

    Subsidiary qualifiers (e.g. 'Web Services', 'Labs', 'America', 'Holdings') are
    intentionally retained so distinct corporate entities never fuzzy-join.
    """
    normalized = unicodedata.normalize("NFKC", name).casefold()
    tokens = [token for token in re.split(r"[^a-z0-9&+#]+", normalized) if token]
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def match_company_aliases(
    employer_names: Iterable[str],
    companies: Iterable[str],
    explicit_aliases: dict[str, str] | None = None,
) -> dict[str, str]:
    """Deterministically map official legal entity names to existing JobPing companies.

    Explicit reviewed aliases take precedence. Remaining names match only when their
    legal-suffix-stripped canonical form equals a unique JobPing company's canonical form.
    """
    by_canonical: dict[str, list[str]] = {}
    exact_names: dict[str, str] = {}
    for company in companies:
        cleaned = company.strip()
        if not cleaned:
            continue
        exact_names[cleaned.casefold()] = cleaned
        key = canonical_company_key(cleaned)
        if key:
            by_canonical.setdefault(key, []).append(cleaned)

    resolved: dict[str, str] = {}
    if explicit_aliases:
        for legal_name, target in explicit_aliases.items():
            if legal_name.strip() and target.strip():
                resolved[legal_name.strip()] = target.strip()

    for raw_name in employer_names:
        legal = raw_name.strip()
        if not legal or legal in resolved:
            continue
        if legal.casefold() in exact_names:
            resolved[legal] = exact_names[legal.casefold()]
            continue
        key = canonical_company_key(legal)
        candidates = by_canonical.get(key, [])
        if len(candidates) == 1:
            resolved[legal] = candidates[0]
    return resolved


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    raw_val = (
        (cell[0].text or "").strip()
        if len(cell) == 1 and cell[0].tag.endswith("}v")
        else "".join(cell.itertext()).strip()
    )
    if cell_type == "s" and raw_val.isdigit():
        idx = int(raw_val)
        return shared_strings[idx] if idx < len(shared_strings) else raw_val
    return raw_val


def _iter_xlsx_rows(path: Path, target_headers: set[str]) -> Iterator[dict[str, str]]:
    """Stream only the required columns from an XLSX workbook using stdlib XML iterparse."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        sheets = sorted(
            name
            for name in names
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        if not sheets:
            raise ValueError(f"No worksheet found in {path.name}")
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            with archive.open("xl/sharedStrings.xml") as handle:
                for _, elem in ET.iterparse(handle, events=("end",)):
                    if elem.tag.endswith("}si"):
                        shared_strings.append(
                            (elem[0].text or "")
                            if len(elem) == 1 and elem[0].tag.endswith("}t")
                            else "".join(elem.itertext())
                        )
                        elem.clear()

        col_to_header: dict[str, str] = {}
        target_count = 0
        with archive.open(sheets[0]) as handle:
            for _, elem in ET.iterparse(handle, events=("end",)):
                if not elem.tag.endswith("}row"):
                    continue
                if not col_to_header:
                    for cell in elem:
                        col = cell.attrib.get("r", "").rstrip("0123456789")
                        if not col:
                            continue
                        val = _cell_text(cell, shared_strings)
                        header = val.strip().upper().replace(" ", "_")
                        if header in target_headers:
                            col_to_header[col] = header
                    elem.clear()
                    if not col_to_header:
                        raise ValueError(f"Required headers not found in {path.name}")
                    target_count = len(col_to_header)
                    continue

                row_values: dict[str, str] = {}
                for cell in elem:
                    col = cell.attrib.get("r", "").rstrip("0123456789")
                    header = col_to_header.get(col)
                    if header is None:
                        continue
                    row_values[header] = _cell_text(cell, shared_strings)
                    if len(row_values) == target_count:
                        break
                elem.clear()
                if row_values:
                    yield row_values


def _iter_official_rows(path: Path, provider: Literal["dol", "uscis"]) -> Iterator[dict[str, str]]:
    target_headers = (
        {"CASE_NUMBER", "CASE_STATUS", "VISA_CLASS", "EMPLOYER_NAME"}
        if provider == "dol"
        else {"FISCAL_YEAR", "EMPLOYER_NAME", "INITIAL_APPROVAL", "CONTINUING_APPROVAL"}
    )
    if path.suffix.casefold() == ".xlsx":
        yield from _iter_xlsx_rows(path, target_headers)
        return
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        for row in rows:
            yield {
                str(key).strip().upper().replace(" ", "_"): (value or "")
                for key, value in row.items()
                if key is not None
            }


def parse_official_csv(
    path: Path,
    *,
    provider: Literal["dol", "uscis"],
    year: int,
    source_url: str,
    aliases: dict[str, str] | None = None,
    companies: Iterable[str] | None = None,
) -> EmployerImport:
    """Read a government CSV or XLSX export; aliases/companies map legal entities to JobPing names.

    Unmapped employers are intentionally excluded; parent/subsidiary names are
    never joined by fuzzy matching. DOL counts cases, not positions or approvals.
    """
    if path.stat().st_size > 500_000_000:
        raise ValueError("Disclosure export exceeds 500 MB")
    raw_counts: dict[str, int] = {}
    seen: set[str] = set()
    for values in _iter_official_rows(path, provider):
        if provider == "dol":
            visa_class = (values.get("VISA_CLASS") or "").strip().upper()
            case_status = (values.get("CASE_STATUS") or "").strip().casefold()
            if visa_class != "H-1B" or case_status != "certified":
                continue
            name = (values.get("EMPLOYER_NAME") or "").strip()
            case = (values.get("CASE_NUMBER") or "").strip()
            if not name or not case or case in seen:
                continue
            seen.add(case)
            count = 1
        else:
            if int(values.get("FISCAL_YEAR", year)) != year:
                continue
            name = (values.get("EMPLOYER_NAME") or "").strip()
            if not name:
                continue
            count = int((values.get("INITIAL_APPROVAL") or "0").replace(",", "")) + int(
                (values.get("CONTINUING_APPROVAL") or "0").replace(",", "")
            )
        if count > 0:
            raw_counts[name] = raw_counts.get(name, 0) + count

    resolved_aliases = (
        match_company_aliases(raw_counts.keys(), companies, explicit_aliases=aliases)
        if companies is not None
        else (aliases or {})
    )
    now = datetime.now(UTC)
    return EmployerImport(
        records=[
            MappedEmployerRecord(
                company=resolved_aliases[name],
                record=EmployerRecord(
                    kind="h1b_filings" if provider == "dol" else "h1b_approvals",
                    year=year,
                    count=count,
                    employer_name=name,
                    source_url=source_url,
                    observed_at=now,
                ),
            )
            for name, count in sorted(raw_counts.items())
            if name in resolved_aliases
        ]
    )


def prepare_dol_directory(
    directory: Path,
    *,
    aliases: dict[str, str] | None = None,
    companies: Iterable[str] | None = None,
    source_url: str = DEFAULT_DOL_SOURCE_URL,
) -> EmployerImport:
    """Aggregate all official DOL disclosure CSV/XLSX files in a directory across fiscal years."""
    if not directory.is_dir():
        raise ValueError(f"Not a directory: {directory}")
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.casefold() in {".csv", ".xlsx"}
    )
    if not files:
        raise ValueError(f"No .csv or .xlsx disclosure files found in {directory}")

    # Collect per-(year, legal_name) counts while deduplicating CASE_NUMBER within each year
    counts_by_year: dict[tuple[int, str], int] = {}
    seen_cases_by_year: dict[int, set[str]] = {}
    all_employer_names: set[str] = set()

    for file_path in files:
        year = infer_fiscal_year(file_path)
        if year is None:
            continue
        if file_path.stat().st_size > 500_000_000:
            raise ValueError(f"Disclosure export {file_path.name} exceeds 500 MB")
        year_seen = seen_cases_by_year.setdefault(year, set())
        for values in _iter_official_rows(file_path, "dol"):
            visa_class = (values.get("VISA_CLASS") or "").strip().upper()
            case_status = (values.get("CASE_STATUS") or "").strip().casefold()
            if visa_class != "H-1B" or case_status != "certified":
                continue
            name = (values.get("EMPLOYER_NAME") or "").strip()
            case = (values.get("CASE_NUMBER") or "").strip()
            if not name or not case or case in year_seen:
                continue
            year_seen.add(case)
            all_employer_names.add(name)
            key = (year, name)
            counts_by_year[key] = counts_by_year.get(key, 0) + 1

    resolved_aliases = (
        match_company_aliases(all_employer_names, companies, explicit_aliases=aliases)
        if companies is not None
        else (aliases or {})
    )
    now = datetime.now(UTC)
    records = [
        MappedEmployerRecord(
            company=resolved_aliases[name],
            record=EmployerRecord(
                kind="h1b_filings",
                year=year,
                count=count,
                employer_name=name,
                source_url=source_url,
                observed_at=now,
            ),
        )
        for (year, name), count in sorted(counts_by_year.items())
        if name in resolved_aliases and count > 0
    ]
    return EmployerImport(records=records)


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
