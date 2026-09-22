"""Deterministic lifecycle decisions based on persisted occurrence evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from app.db.models import JobOccurrence, JobPosting
from app.schemas.job import NormalizedJob
from app.services.hasher import canonicalize_apply_url, generate_content_hash
from app.services.source_identity import (
    stable_posting_identity,
    trustworthy_posting_date_source,
)


class OccurrenceDecision(StrEnum):
    """How one source observation relates to the durable logical-job lifecycle."""

    DISCOVER = "discover"
    OPEN_UPDATE = "open_update"
    CLOSE = "close"
    REOPEN = "reopen"
    REPOST = "repost"
    AMBIGUOUS_REOPEN = "ambiguous_reopen"


OccurrenceEvidence = tuple[JobOccurrence, frozenset[tuple[str, str]]]


def decide_occurrence(
    candidate: NormalizedJob,
    *,
    source: str,
    existing: JobPosting | None,
    evidence: OccurrenceEvidence | None,
) -> OccurrenceDecision:
    """Resolve lifecycle state once, using stable identity and confirmed closure.

    An open observation can reactivate a closed occurrence only when it proves the
    same stable ATS identity. A new comparable identity (or the direct-ATS
    date-and-URL fallback) proves a repost. All weaker evidence is ambiguous and
    must leave the durable closure intact.
    """
    if existing is None:
        return OccurrenceDecision.DISCOVER
    if candidate.is_closed:
        return (
            OccurrenceDecision.CLOSE if not existing.is_closed else OccurrenceDecision.OPEN_UPDATE
        )
    if not existing.is_closed:
        return OccurrenceDecision.OPEN_UPDATE
    if evidence is None:
        return OccurrenceDecision.AMBIGUOUS_REOPEN

    occurrence, previous_identities = evidence
    incoming_identity = _candidate_identity(candidate)
    comparable = {
        identity
        for identity in previous_identities
        if incoming_identity and identity[0] == incoming_identity[0]
    }
    if incoming_identity is not None and incoming_identity in comparable:
        if occurrence.closed_at is None:
            # A concurrent writer already created/reactivated this exact latest
            # occurrence; its row wins over a stale posting flag.
            return OccurrenceDecision.OPEN_UPDATE
        return OccurrenceDecision.REOPEN
    if occurrence.closed_at is None:
        return OccurrenceDecision.AMBIGUOUS_REOPEN
    if is_repost_candidate(candidate, source=source, existing=existing, evidence=evidence):
        return OccurrenceDecision.REPOST
    return OccurrenceDecision.AMBIGUOUS_REOPEN


def prepare_candidate(
    candidate: NormalizedJob,
    decision: OccurrenceDecision,
    *,
    existing: JobPosting | None,
) -> NormalizedJob:
    """Apply the decision to an effective state projection for Redis and results."""
    if decision is OccurrenceDecision.REPOST:
        return candidate.model_copy(update={"occurrence_kind": "reposted"})
    if decision is not OccurrenceDecision.AMBIGUOUS_REOPEN or existing is None:
        return candidate.model_copy(update={"occurrence_kind": None})

    # Keep the SQL and Redis representation aligned with the confirmed closed
    # state. The repository records raw source URL/date on provenance separately.
    persisted_url = canonicalize_apply_url(existing.apply_url)
    return candidate.model_copy(
        update={
            "apply_url": persisted_url,
            "content_hash": existing.content_hash
            or generate_content_hash(existing.base_hash, persisted_url, existing.location, True),
            "location": existing.location,
            "is_closed": True,
            "occurrence_kind": None,
        }
    )


def is_repost_candidate(
    candidate: NormalizedJob,
    *,
    source: str,
    existing: JobPosting,
    evidence: OccurrenceEvidence,
) -> bool:
    """Require confirmed closure plus a new comparable ID or trusted date/URL."""
    occurrence, previous_identities = evidence
    if not existing.is_closed or occurrence.closed_at is None or candidate.is_closed:
        return False

    incoming_identity = _candidate_identity(candidate)
    comparable_identities = {
        identity
        for identity in previous_identities
        if incoming_identity and identity[0] == incoming_identity[0]
    }
    if incoming_identity is not None and previous_identities:
        if not comparable_identities or incoming_identity in comparable_identities:
            return False
        if candidate.posted_at is not None and occurrence.posted_at is not None:
            return _as_utc(candidate.posted_at) > _as_utc(occurrence.posted_at)
        return True

    if not trustworthy_posting_date_source(source):
        return False
    previous_posted_at = occurrence.posted_at or existing.posted_at
    if candidate.posted_at is None or previous_posted_at is None:
        return False
    if _as_utc(candidate.posted_at) <= _as_utc(previous_posted_at):
        return False
    return canonicalize_apply_url(str(candidate.apply_url)) != canonicalize_apply_url(
        occurrence.apply_url
    )


def is_same_repost_occurrence(candidate: NormalizedJob, occurrence: JobOccurrence) -> bool:
    """Detect that another worker already created the same repost occurrence."""
    if occurrence.kind != "reposted":
        return False
    candidate_identity = _candidate_identity(candidate)
    occurrence_identity = (
        (occurrence.identity_namespace, occurrence.external_job_id)
        if occurrence.identity_namespace and occurrence.external_job_id
        else stable_posting_identity(
            source=occurrence.source,
            source_id=occurrence.source_id,
            apply_url=occurrence.apply_url,
            payload={},
        )
    )
    if candidate_identity is not None or occurrence_identity is not None:
        return candidate_identity is not None and candidate_identity == occurrence_identity
    return (
        candidate.posted_at is not None
        and occurrence.posted_at is not None
        and _as_utc(candidate.posted_at) == _as_utc(occurrence.posted_at)
        and canonicalize_apply_url(str(candidate.apply_url))
        == canonicalize_apply_url(occurrence.apply_url)
    )


def _candidate_identity(candidate: NormalizedJob) -> tuple[str, str] | None:
    if candidate.identity_namespace and candidate.external_job_id:
        return candidate.identity_namespace, candidate.external_job_id
    return stable_posting_identity(
        source=candidate.source,
        source_id=candidate.source_id,
        apply_url=str(candidate.apply_url),
        payload={},
    )


def _as_utc(value: datetime) -> datetime:
    """Treat SQLite's naive round-trip timestamps as UTC for comparison."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
