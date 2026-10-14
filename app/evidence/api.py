"""Cached evidence reads and account-private screening; no HTTP fetch in API requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.api.deps import get_db
from app.api.v1.jobs import _apply_filters
from app.db.models import (
    EvidenceCheck,
    EvidenceDocument,
    JobOccurrence,
    JobPosting,
    ScreeningProfile,
)
from app.evidence.schemas import VERSION, Bucket, Requirements
from app.evidence.screening import bucket_expression, evaluate, utc
from app.evidence.store import current_occurrence, enqueue
from app.notifications.security import Identity
from app.request_limits.dependencies import limited_identity, private_db
from app.schemas.api import JobResponse

router = APIRouter(tags=["evidence"])


async def verified(who: Annotated[Identity, Depends(limited_identity)]) -> Identity:
    if not who.verified:
        raise HTTPException(403, "Verify your email to use private screening")
    return who


Who = Annotated[Identity, Depends(verified)]
DB = Annotated[AsyncSession, Depends(private_db)]
PublicDB = Annotated[AsyncSession, Depends(get_db)]


def private(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


@router.get("/me/screening-profile")
async def get_profile(who: Who, db: DB, response: Response) -> dict:
    private(response)
    profile = await db.get(ScreeningProfile, who.uid)
    return {
        "configured": bool(
            profile and Requirements.model_validate(profile.requirements).configured
        ),
        "requirements": profile.requirements if profile else Requirements().model_dump(mode="json"),
        "revision": profile.revision if profile else 0,
    }


@router.put("/me/screening-profile")
async def put_profile(request: Request, who: Who, db: DB, response: Response) -> dict:
    private(response)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, "Screening requirements exceed the size limit")
    try:
        requirements = Requirements.model_validate_json(raw)
    except ValidationError:
        # FastAPI's ordinary validation response includes input; keep private facts out.
        raise HTTPException(422, "Invalid screening requirements") from None
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(ScreeningProfile)
        .values(user_id=who.uid, requirements=Requirements().model_dump(mode="json"), revision=0)
        .on_conflict_do_nothing(index_elements=[ScreeningProfile.user_id])
    )
    profile = await db.scalar(
        select(ScreeningProfile).where(ScreeningProfile.user_id == who.uid).with_for_update()
    )
    assert profile is not None
    if profile.revision == 0 or profile.requirements != requirements.model_dump(mode="json"):
        profile.requirements = requirements.model_dump(mode="json")
        profile.revision += 1
    return {
        "configured": requirements.configured,
        "requirements": profile.requirements,
        "revision": profile.revision,
    }


@router.delete("/me/screening-profile")
async def delete_profile(who: Who, db: DB, response: Response) -> dict:
    private(response)
    profile = await db.scalar(
        select(ScreeningProfile).where(ScreeningProfile.user_id == who.uid).with_for_update()
    )
    if profile:
        await db.delete(profile)
    return {"deleted": True}


def screening(
    job: JobPosting, check: EvidenceCheck | None, profile: Requirements | None, now: datetime
) -> dict:
    return evaluate(
        profile,
        check.projection if check else None,
        kind=job.job_type.value,
        status=check.status if check else None,
        verified_at=check.verified_at if check else None,
        now=now,
        closed=job.is_closed,
        target_current=bool(
            check
            and check.target["url"] == job.apply_url
            and check.target["location"] == job.location
            and check.target.get("title") == job.title
        ),
    )


async def evidence_view(db: AsyncSession, job: JobPosting, check: EvidenceCheck | None) -> dict:
    documents = []
    if check and check.document_ids:
        rows = await db.scalars(
            select(EvidenceDocument).where(EvidenceDocument.id.in_(check.document_ids))
        )
        documents = [
            {
                "id": row.id,
                "source_url": row.source_url,
                "final_url": row.final_url,
                "kind": row.kind,
                "observed_at": row.observed_at,
                "facts": row.facts,
                "spans": row.spans,
                "extraction_version": row.extraction_version,
            }
            for row in rows
        ]
    current = bool(
        check
        and check.target["url"] == job.apply_url
        and check.target["location"] == job.location
        and check.target.get("title") == job.title
    )
    fresh = bool(
        check
        and check.status == "complete"
        and check.verified_at
        and current
        and check.projection
        and check.projection.get("extraction_version") == VERSION
        and utc(check.verified_at) > datetime.now(UTC) - timedelta(hours=24)
    )
    return {
        "job_id": job.id,
        "occurrence_id": check.occurrence_id if check else None,
        "status": check.status if check else "not_checked",
        "fresh": fresh,
        "checked_at": check.checked_at if check else None,
        "verified_at": check.verified_at if check else None,
        "error_code": check.error_code if check else None,
        "details": check.projection if check else None,
        "documents": documents,
    }


@router.get("/jobs/{job_id}/evidence")
async def get_evidence(job_id: int, db: PublicDB, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    job = await db.get(JobPosting, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    occurrence = await current_occurrence(db, job_id)
    check = await db.get(EvidenceCheck, occurrence.id) if occurrence else None
    return await evidence_view(db, job, check)


@router.post("/me/jobs/{job_id}/evidence-check")
async def request_check(job_id: int, who: Who, db: DB, response: Response) -> dict:
    private(response)
    job = await db.scalar(select(JobPosting).where(JobPosting.id == job_id).with_for_update())
    if not job:
        raise HTTPException(404, "Job not found")
    occurrence = await current_occurrence(db, job_id)
    if job.is_closed or not occurrence or occurrence.closed_at:
        raise HTTPException(409, "This occurrence is not open for applications")
    check = await enqueue(db, job, occurrence, datetime.now(UTC), refresh=True)
    assert check is not None
    response.status_code = 202 if check.status in {"queued", "running"} else 200
    stored = await db.get(ScreeningProfile, who.uid)
    profile = Requirements.model_validate(stored.requirements) if stored else None
    return {
        **await evidence_view(db, job, check),
        "screening": screening(job, check, profile, datetime.now(UTC)),
    }


@router.get("/me/jobs")
async def private_jobs(
    who: Who,
    db: DB,
    response: Response,
    bucket: Bucket = "matches",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=500)] = None,
    company: Annotated[str | None, Query(max_length=255)] = None,
    active: bool | None = True,
) -> dict:
    private(response)
    stored = await db.get(ScreeningProfile, who.uid)
    profile = Requirements.model_validate(stored.requirements) if stored else None
    now = datetime.now(UTC)
    latest = (
        select(func.max(JobOccurrence.id))
        .where(JobOccurrence.job_id == JobPosting.id)
        .correlate(JobPosting)
        .scalar_subquery()
    )
    statement = _apply_filters(
        select(JobPosting, EvidenceCheck)
        .join(JobPosting.company)
        .outerjoin(EvidenceCheck, EvidenceCheck.occurrence_id == latest),
        search=search,
        company=company,
        active=active,
    )
    predicate = bucket_expression(profile, dialect=db.get_bind().dialect.name, now=now)
    counts = {
        name: int(
            await db.scalar(
                select(func.count()).select_from(statement.where(predicate == name).subquery())
            )
            or 0
        )
        for name in ("matches", "review", "excluded")
    }
    selected = statement.where(predicate == bucket).options(joinedload(JobPosting.company))
    rows = (
        await db.execute(
            selected.order_by(
                func.coalesce(JobPosting.posted_at, JobPosting.created_at).desc(),
                JobPosting.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = [
        {
            **JobResponse.model_validate(job).model_dump(mode="json"),
            "screening": screening(job, check, profile, now),
            "verification": {
                "status": check.status if check else "not_checked",
                "verified_at": check.verified_at if check else None,
            },
        }
        for job, check in rows
    ]
    return {
        "items": items,
        "total": counts[bucket],
        "counts": counts,
        "page": page,
        "page_size": page_size,
        "total_pages": (counts[bucket] + page_size - 1) // page_size,
        "profile_configured": bool(profile and profile.configured),
        "profile_revision": stored.revision if stored else 0,
    }
