"""Deterministic, conservative repost evidence rules."""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.models import JobOccurrence, JobPosting
from app.schemas.job import NormalizedJob
from app.services.hasher import canonicalize_apply_url
from app.services.source_identity import (
    stable_posting_identity,
    trustworthy_posting_date_source,
)


def is_repost_candidate(
    candidate: NormalizedJob,
    *,
    source: str,
    existing: JobPosting,
    evidence: tuple[JobOccurrence, frozenset[tuple[str, str]]],
) -> bool:
    """Require confirmed closure plus a new ATS ID or trusted newer date and URL."""
    occurrence, previous_identities = evidence
    if not existing.is_closed or occurrence.closed_at is None or candidate.is_closed:
        return False

    incoming_identity = (
        (candidate.identity_namespace, candidate.external_job_id)
        if candidate.identity_namespace and candidate.external_job_id
        else None
    )
    comparable_identities = {
        identity
        for identity in previous_identities
        if incoming_identity and identity[0] == incoming_identity[0]
    }
    if incoming_identity is not None and previous_identities:
        if not comparable_identities or incoming_identity in comparable_identities:
            # Different provider/tenant identifiers are not comparable, while the
            # same known requisition ID is positive evidence against a repost.
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
    """Detect that a repost candidate is already represented by the latest occurrence.

    This second check runs after the posting upsert. It makes two transactions that
    classified against the same prior closed row converge on the occurrence created
    by the transaction that committed first.
    """
    if occurrence.kind != "reposted":
        return False
    candidate_identity = (
        (candidate.identity_namespace, candidate.external_job_id)
        if candidate.identity_namespace and candidate.external_job_id
        else stable_posting_identity(
            source=candidate.source,
            source_id=candidate.source_id,
            apply_url=str(candidate.apply_url),
            payload={},
        )
    )
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


def _as_utc(value: datetime) -> datetime:
    """Treat SQLite's naive round-trip timestamps as UTC for deterministic comparison."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
