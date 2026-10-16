"""Transactional queue coalescing, expiring claims and occurrence fencing."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Company, EvidenceCheck, EvidenceDocument, JobOccurrence, JobPosting
from app.evidence.extract import extract, reconcile
from app.evidence.schemas import VERSION, Document
from app.evidence.screening import projection, utc
from app.services.source_identity import stable_posting_identity


async def current_occurrence(session: AsyncSession, job_id: int) -> JobOccurrence | None:
    return await session.scalar(
        select(JobOccurrence)
        .where(JobOccurrence.job_id == job_id)
        .order_by(JobOccurrence.id.desc())
        .limit(1)
    )


async def target_for(session: AsyncSession, job: JobPosting, occurrence: JobOccurrence) -> dict:
    company = job.company if "company" in job.__dict__ else None
    if company is None:
        company = await session.get(Company, job.company_id)
    identity = stable_posting_identity(
        source=occurrence.source,
        source_id=occurrence.source_id,
        apply_url=job.apply_url,
        payload={},
    )
    if identity is None and occurrence.identity_namespace and occurrence.external_job_id:
        identity = (occurrence.identity_namespace, occurrence.external_job_id)
    return {
        "url": job.apply_url,
        "location": job.location,
        "company": company.name if company else "",
        "title": job.title,
        "namespace": identity[0] if identity else None,
        "external_id": identity[1] if identity else None,
    }


async def enqueue(
    session: AsyncSession,
    job: JobPosting,
    occurrence: JobOccurrence,
    now: datetime,
    *,
    refresh: bool = False,
    invalidate: bool = False,
    observation_source: str | None = None,
    observation_details: dict | None = None,
) -> EvidenceCheck | None:
    if job.is_closed or occurrence.closed_at is not None:
        return None
    target = await target_for(session, job, occurrence)
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    await session.execute(
        insert(EvidenceCheck)
        .values(
            occurrence_id=occurrence.id,
            target=target,
            status="queued",
            requested_at=now,
            available_at=now,
            document_ids=[],
            ingestion_checksums={},
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=[EvidenceCheck.occurrence_id])
    )
    check = await session.scalar(
        select(EvidenceCheck)
        .where(EvidenceCheck.occurrence_id == occurrence.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert check is not None
    changed = check.target != target
    recognized = {
        "greenhouse",
        "lever",
        "workday",
        "amazon",
        "meta",
        "simplify",
        "github_markdown",
        "applyguy_internships",
        "applyguy_new_grad",
    }
    if observation_source in recognized:
        fingerprint = hashlib.sha256(
            json.dumps(semantic(observation_details), sort_keys=True).encode()
        ).hexdigest()
        invalidate = check.ingestion_checksums.get(observation_source) != fingerprint
        if invalidate:
            check.ingestion_checksums = {
                **check.ingestion_checksums,
                observation_source: fingerprint,
            }
    expired = not check.verified_at or utc(check.verified_at) <= utc(now) - timedelta(hours=24)
    recent = check.verified_at and utc(check.verified_at) > utc(now) - timedelta(hours=1)
    active = check.status in {"queued", "running"} and (
        check.status == "queued" or check.lease_until and utc(check.lease_until) > utc(now)
    )
    if (
        changed
        or invalidate
        or (
            not active
            and utc(check.available_at) <= utc(now)
            and (expired or refresh and not recent)
        )
    ):
        check.target, check.status = target, "queued"
        check.requested_at, check.available_at = now, now
        check.lease_token = check.lease_until = None
        check.error_code = None
    return check


async def backfill(session: AsyncSession, now: datetime, limit: int = 50) -> int:
    latest = (
        select(func.max(JobOccurrence.id))
        .where(JobOccurrence.job_id == JobPosting.id)
        .correlate(JobPosting)
        .scalar_subquery()
    )
    jobs = await session.scalars(
        select(JobPosting)
        .options(joinedload(JobPosting.company))
        .outerjoin(EvidenceCheck, EvidenceCheck.occurrence_id == latest)
        .where(
            JobPosting.is_closed.is_(False),
            or_(
                EvidenceCheck.occurrence_id.is_(None),
                (EvidenceCheck.status.not_in(["queued", "running"]))
                & (EvidenceCheck.available_at <= now)
                & (EvidenceCheck.requested_at <= now - timedelta(hours=1))
                & (
                    or_(
                        EvidenceCheck.verified_at.is_(None),
                        EvidenceCheck.verified_at <= now - timedelta(hours=24),
                    )
                ),
            ),
        )
        .order_by(
            func.coalesce(JobPosting.posted_at, JobPosting.created_at).desc(), JobPosting.id.desc()
        )
        .limit(limit)
    )
    count = 0
    for job in jobs:
        occurrence = await current_occurrence(session, job.id)
        if occurrence and await enqueue(session, job, occurrence, now):
            count += 1
    return count


async def claim(session: AsyncSession, now: datetime) -> tuple[int, str, dict] | None:
    row = await session.scalar(
        select(EvidenceCheck)
        .where(
            EvidenceCheck.available_at <= now,
            or_(
                EvidenceCheck.status == "queued",
                (EvidenceCheck.status == "running") & (EvidenceCheck.lease_until <= now),
            ),
        )
        .order_by(EvidenceCheck.requested_at, EvidenceCheck.occurrence_id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if row is None:
        return None
    token = str(uuid.uuid4())
    eligible = or_(
        EvidenceCheck.status == "queued",
        (EvidenceCheck.status == "running") & (EvidenceCheck.lease_until <= now),
    )
    result = await session.execute(
        update(EvidenceCheck)
        .where(EvidenceCheck.occurrence_id == row.occurrence_id, eligible)
        .values(
            status="running",
            lease_token=token,
            lease_until=now + timedelta(seconds=90),
            attempts=EvidenceCheck.attempts + 1,
        )
        .execution_options(synchronize_session=False)
    )
    return (row.occurrence_id, token, dict(row.target)) if result.rowcount else None


def semantic(value: object) -> object:
    """Observation timestamps belong to verification, not material job changes."""
    if isinstance(value, dict):
        return {key: semantic(item) for key, item in value.items() if key != "observed_at"}
    if isinstance(value, list):
        return [semantic(item) for item in value]
    return value


async def finish(
    session: AsyncSession,
    occurrence_id: int,
    token: str,
    target: dict,
    documents: list[Document],
    now: datetime,
    *,
    error: str | None = None,
) -> bool:
    occurrence = await session.get(JobOccurrence, occurrence_id)
    if occurrence is None:
        return False
    job = await session.scalar(
        select(JobPosting)
        .where(JobPosting.id == occurrence.job_id)
        .options(joinedload(JobPosting.company))
        .with_for_update(of=JobPosting)
    )
    check = await session.scalar(
        select(EvidenceCheck)
        .where(EvidenceCheck.occurrence_id == occurrence_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not job
        or not check
        or check.lease_token != token
        or check.status != "running"
        or not check.lease_until
        or utc(check.lease_until) <= utc(now)
    ):
        return False
    latest = await current_occurrence(session, job.id)
    if (
        not latest
        or latest.id != occurrence_id
        or job.is_closed
        or occurrence.closed_at is not None
        or target != await target_for(session, job, occurrence)
        or check.target != target
    ):
        check.status, check.error_code = "blocked", "target_changed"
        check.lease_token = check.lease_until = None
        check.available_at = now + timedelta(hours=1)
        return False
    check.checked_at = now
    check.lease_token = check.lease_until = None
    pairs: list[tuple[Document, dict]] = []
    ids: list[int] = []
    for document in documents:
        if document.role_identity != target:
            raise ValueError("Evidence document target mismatch")
        facts, spans = extract(document)
        pairs.append((document, facts))
        checksum = hashlib.sha256(
            json.dumps(
                {"text": document.text, "pay": document.structured_pay},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        existing = await session.scalar(
            select(EvidenceDocument).where(
                EvidenceDocument.occurrence_id == occurrence_id,
                EvidenceDocument.source_url == document.source_url,
                EvidenceDocument.checksum == checksum,
                EvidenceDocument.extraction_version == VERSION,
            )
        )
        if existing is None:
            existing = EvidenceDocument(
                occurrence_id=occurrence_id,
                source_url=document.source_url,
                final_url=document.final_url,
                kind=document.kind,
                checksum=checksum,
                extraction_version=VERSION,
                observed_at=document.observed_at,
                text=document.text,
                facts=facts,
                spans=spans,
                role_identity=target,
                complete=document.complete,
            )
            session.add(existing)
            await session.flush()
        ids.append(existing.id)
    data = projection(reconcile(pairs), target["location"])
    if error or not documents or not all(document.complete for document in documents):
        check.status = (
            "partial"
            if documents
            else (
                "blocked"
                if error
                in {
                    "verification_barrier",
                    "employer_not_reviewed",
                    "identity_mismatch",
                    "role_scope_unverified",
                }
                else "failed"
            )
        )
        check.error_code = error or "incomplete_documents"
        check.available_at = now + timedelta(hours=1)
        if check.projection is None and documents:
            check.projection, check.document_ids = data, ids
        return True
    check.projection, check.document_ids = data, ids
    check.status, check.error_code = "complete", None
    check.verified_at, check.available_at = now, now
    details = {key: data[key] for key in ("policies", "compensation")}
    # Screening-only normalization must not leak extra fields into strict legacy DTOs.
    details["compensation"] = [
        {key: value for key, value in pay.items() if key not in {"us", "location_key"}}
        for pay in details["compensation"]
    ]
    if semantic(job.details) != semantic(details):
        job.details = details
        job.updated_at = now
        occurrence.details = details
    return True
