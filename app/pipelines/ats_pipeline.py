"""Persistence pipeline for standardized direct ATS scrapers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repository import DatabaseRepository
from app.schemas.job import JobType, NormalizedJob, RawJobPayload
from app.scrapers.base import BaseScraper
from app.services.deduplicator import DeduplicationState, JobDeduplicator
from app.services.hasher import (
    canonicalize_apply_url,
    choose_canonical_apply_url,
    generate_base_hash,
    generate_content_hash,
)
from app.services.posting_dates import parse_source_posted_at
from app.services.repost_classifier import (
    OccurrenceDecision,
    decide_occurrence,
    prepare_candidate,
)
from app.services.source_identity import stable_posting_identity


class Deduplicator(Protocol):
    """Structural contract used by the pipeline and deterministic test doubles."""

    async def classify_and_update(
        self, *, base_hash: str, content_hash: str, is_closed: bool
    ) -> DeduplicationState: ...


@dataclass(frozen=True, slots=True)
class ATSOutcome:
    """One ATS row's SQL-authoritative lifecycle result.

    ``NEW_ROLE`` creates the first discovery, ``ROLE_UPDATED`` changes an existing
    role without a repost, ``ROLE_CLOSED`` closes the active occurrence,
    ``ROLE_REPOSTED`` creates a repost occurrence, and ``NO_OP`` makes no
    authoritative lifecycle transition.
    """

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
        session: AsyncSession | None,
        *,
        season: int,
        job_type: JobType,
    ) -> None:
        if season not in {2026, 2027}:
            raise ValueError("season must be 2026 or 2027")
        self._scrapers = tuple(scrapers)
        self._deduplicator = deduplicator
        self._session = session
        self._repository = DatabaseRepository(session) if session is not None else None
        self._season = season
        self._job_type = job_type

    async def run(self) -> ATSPipelineResult:
        """Run each scraper and persist non-no-op classifications."""
        result = ATSPipelineResult()
        seen: set[str] = set()
        candidates: list[tuple[RawJobPayload, NormalizedJob]] = []
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
                candidates.append((raw, job))

        if self._session is not None and not self._session.in_transaction():
            async with self._session.begin():
                await self._classify_and_persist(result, candidates)
        else:
            await self._classify_and_persist(result, candidates)
        return result

    async def _classify_and_persist(
        self,
        result: ATSPipelineResult,
        candidates: list[tuple[RawJobPayload, NormalizedJob]],
    ) -> None:
        """Batch-reconcile, classify, and persist candidates in one SQL transaction."""
        existing_by_hash = (
            await self._repository.get_job_postings_by_base_hashes(
                [job.base_hash for _, job in candidates]
            )
            if self._repository is not None
            else {}
        )
        occurrence_evidence = (
            await self._repository.get_current_occurrence_evidence(
                [job.base_hash for _, job in candidates]
            )
            if self._repository is not None
            else {}
        )
        pending: list[NormalizedJob] = []
        staged: list[tuple[RawJobPayload, DeduplicationState, NormalizedJob]] = []
        for raw, candidate in candidates:
            existing = existing_by_hash.get(candidate.base_hash)
            evidence = occurrence_evidence.get(candidate.base_hash)
            decision = decide_occurrence(
                candidate,
                source=raw.source,
                existing=existing,
                evidence=evidence,
            )
            effective_candidate = prepare_candidate(candidate, decision, existing=existing)
            reposted = decision is OccurrenceDecision.REPOST
            job = self._reconcile_persisted_url(
                effective_candidate,
                None if reposted or existing is None else existing.apply_url,
            )
            state = await self._deduplicator.classify_and_update(
                base_hash=job.base_hash,
                content_hash=job.content_hash,
                is_closed=job.is_closed,
            )
            if reposted:
                state = DeduplicationState.ROLE_REPOSTED
            elif self._repository is not None:
                # Redis is a cache. PostgreSQL remains authoritative when it has a
                # logical job, and a warm Redis key must not suppress database repair.
                if existing is None:
                    state = DeduplicationState.NEW_ROLE
                elif existing.is_closed and not job.is_closed:
                    state = DeduplicationState.ROLE_UPDATED
                elif not existing.is_closed and job.is_closed:
                    state = DeduplicationState.ROLE_CLOSED
                elif existing.content_hash == job.content_hash:
                    state = DeduplicationState.NO_OP
                elif state in {DeduplicationState.NO_OP, DeduplicationState.NEW_ROLE}:
                    state = DeduplicationState.ROLE_UPDATED
            # Persist every coalesced observation in one batch. NO_OP rows repair
            # provenance and let PostgreSQL repair a warm-cache/database mismatch.
            # Persist the raw normalized observation. The repository reruns the
            # shared decision while holding SQL locks and applies effective state
            # there, avoiding a stale pre-read being mistaken for closure evidence.
            pending.append(candidate)
            staged.append((raw, state, job))
        if pending and self._repository is not None:
            persisted = await self._repository.bulk_upsert_job_postings_with_outcomes(pending)
            for (raw, _, job), persisted_result in zip(staged, persisted, strict=True):
                posting = persisted_result.posting
                await self._deduplicator.classify_and_update(
                    base_hash=posting.base_hash,
                    content_hash=posting.content_hash,
                    is_closed=posting.is_closed,
                )
                job = job.model_copy(
                    update={
                        "occurrence_kind": (
                            "reposted"
                            if persisted_result.state is DeduplicationState.ROLE_REPOSTED
                            else None
                        )
                    }
                )
                result.outcomes.append(
                    ATSOutcome(raw.source, raw.source_id, persisted_result.state, job)
                )
        else:
            result.outcomes.extend(
                ATSOutcome(raw.source, raw.source_id, state, job) for raw, state, job in staged
            )

    def _normalize(self, raw: RawJobPayload) -> NormalizedJob:
        company = (raw.company or "").strip()
        title = (raw.title or "").strip()
        apply_url = canonicalize_apply_url((raw.apply_url or "").strip())
        location = self._location(raw.location)
        closed = self._closed(raw.is_closed)
        base_hash = generate_base_hash(company, title)
        content_hash = generate_content_hash(base_hash, apply_url, location, closed)
        observed_at = raw.observed_at or datetime.now(UTC)
        posted_at = parse_source_posted_at(
            raw.payload.get("posted", raw.payload.get("date_posted")),
            observed_at=observed_at,
        )
        identity = stable_posting_identity(
            source=raw.source,
            source_id=raw.source_id,
            apply_url=apply_url,
            payload=raw.payload,
        )
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
            posted_at=posted_at,
            observed_at=observed_at,
            source=raw.source,
            source_id=raw.source_id,
            identity_namespace=identity[0] if identity else None,
            external_job_id=identity[1] if identity else None,
        )

    @staticmethod
    def _reconcile_persisted_url(job: NormalizedJob, existing_url: str | None) -> NormalizedJob:
        """Resolve the effective URL before Redis sees the content state."""
        incoming_url = str(job.apply_url)
        effective_url = choose_canonical_apply_url(existing_url or "", incoming_url)
        if effective_url == incoming_url:
            return job
        content_hash = generate_content_hash(
            job.base_hash,
            effective_url,
            job.location,
            job.is_closed,
        )
        return job.model_copy(update={"apply_url": effective_url, "content_hash": content_hash})

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
