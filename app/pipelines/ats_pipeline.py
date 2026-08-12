"""Persistence pipeline for standardized direct ATS scrapers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import DatabaseRepository
from app.schemas.job import JobType, NormalizedJob, RawJobPayload
from app.scrapers.base import BaseScraper
from app.services.deduplicator import DeduplicationState, JobDeduplicator
from app.services.hasher import generate_base_hash, generate_content_hash


class Deduplicator(Protocol):
    """Structural contract used by the pipeline and deterministic test doubles."""

    async def classify_and_update(
        self, *, base_hash: str, content_hash: str, is_closed: bool
    ) -> DeduplicationState: ...


@dataclass(frozen=True, slots=True)
class ATSOutcome:
    """One successfully classified ATS row."""

    source: str
    source_id: str | None
    state: DeduplicationState
    job: NormalizedJob


@dataclass(frozen=True, slots=True)
class ATSRejection:
    """A row rejected at the normalization boundary."""

    source: str
    source_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ATSScraperFailure:
    """A scraper that failed before yielding rows."""

    scraper: str
    company: str
    error_type: str
    reason: str


@dataclass(slots=True)
class ATSPipelineResult:
    """Aggregate result for one ordered group of scraper runs."""

    outcomes: list[ATSOutcome] = field(default_factory=list)
    rejected: list[ATSRejection] = field(default_factory=list)
    failures: list[ATSScraperFailure] = field(default_factory=list)
    duplicates: list[ATSRejection] = field(default_factory=list)

    def categorized(self, state: DeduplicationState) -> tuple[ATSOutcome, ...]:
        return tuple(item for item in self.outcomes if item.state is state)


class ATSPipeline:
    """Normalize, deduplicate, and persist Greenhouse/Lever scraper output.

    Scrapers and their rows are processed in caller order; the first occurrence
    of a base identity wins. Redis classification necessarily precedes the SQL
    transaction. A SQL failure is raised (never hidden), but the current
    deduplicator API has no compare-and-restore primitive, so its cache may be
    ahead of PostgreSQL until TTL expiry or a later reconciliation pass.
    """

    def __init__(
        self,
        scrapers: list[BaseScraper],
        deduplicator: JobDeduplicator | Deduplicator,
        session: AsyncSession,
        *,
        season: int,
        job_type: JobType,
    ) -> None:
        if season not in {2026, 2027}:
            raise ValueError("season must be 2026 or 2027")
        self._scrapers = tuple(scrapers)
        self._deduplicator = deduplicator
        self._session = session
        self._repository = DatabaseRepository(session)
        self._season = season
        self._job_type = job_type

    async def run(self) -> ATSPipelineResult:
        """Run each scraper and persist non-no-op classifications."""
        result = ATSPipelineResult()
        seen: set[str] = set()
        pending: list[NormalizedJob] = []
        for scraper in self._scrapers:
            try:
                rows = await scraper.run()
            except Exception as exc:
                result.failures.append(
                    ATSScraperFailure(
                        scraper=scraper.scraper_name,
                        company=scraper.company,
                        error_type=type(exc).__name__,
                        reason=str(exc),
                    )
                )
                continue
            for raw in rows:
                try:
                    job = self._normalize(raw)
                except (ValidationError, TypeError, ValueError) as exc:
                    result.rejected.append(ATSRejection(raw.source, raw.source_id, str(exc)))
                    continue
                if job.base_hash in seen:
                    result.duplicates.append(
                        ATSRejection(raw.source, raw.source_id, "duplicate base identity in run")
                    )
                    continue
                seen.add(job.base_hash)
                async with self._session.begin():
                    previous = await self._repository.get_job_posting_by_base_hash(job.base_hash)
                    state = await self._deduplicator.classify_and_update(
                        base_hash=job.base_hash,
                        content_hash=job.content_hash,
                        is_closed=job.is_closed,
                    )
                    if state is DeduplicationState.NO_OP:
                        result.outcomes.append(ATSOutcome(raw.source, raw.source_id, state, job))
                        continue
                    previous_state = self._database_state(previous)
                    posting = await self._repository.save_job_posting(job)
                    new_state = "CLOSED" if job.is_closed else "OPEN"
                    if previous_state != new_state:
                        await self._repository.log_status_change(
                            posting.id, previous_state, new_state
                        )
                state = await self._deduplicator.classify_and_update(
                    base_hash=job.base_hash,
                    content_hash=job.content_hash,
                    is_closed=job.is_closed,
                )
                if state is not DeduplicationState.NO_OP:
                    pending.append(job)
                result.outcomes.append(ATSOutcome(raw.source, raw.source_id, state, job))
        if pending:
            await self._repository.bulk_upsert_job_postings(pending)
        return result

    def _normalize(self, raw: RawJobPayload) -> NormalizedJob:
        company = (raw.company or "").strip()
        title = (raw.title or "").strip()
        apply_url = (raw.apply_url or "").strip()
        location = self._location(raw.location)
        closed = self._closed(raw.is_closed)
        base_hash = generate_base_hash(company, title)
        content_hash = generate_content_hash(base_hash, apply_url, location, closed)
        return NormalizedJob(
            company_name=company,
            title=title,
            base_hash=base_hash,
            content_hash=content_hash,
            apply_url=apply_url,
            location=location,
            season=self._season,
            job_type=self._job_type,
            is_closed=closed,
        )

    @staticmethod
    def _location(value: str | list[str] | None) -> str:
        values = value if isinstance(value, list) else [value or ""]
        normalized = list(dict.fromkeys(" ".join(item.split()) for item in values if item.strip()))
        return "; ".join(normalized) or "Unspecified"

    @staticmethod
    def _closed(value: bool | str | int | None) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value == 1
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"true", "1", "yes", "closed"}:
                return True
            if normalized in {"false", "0", "no", "open", ""}:
                return False
        if value is None:
            return False
        raise ValueError("is_closed must be a recognizable boolean value")


__all__ = [
    "ATSOutcome",
    "ATSPipeline",
    "ATSPipelineResult",
    "ATSRejection",
    "ATSScraperFailure",
]
