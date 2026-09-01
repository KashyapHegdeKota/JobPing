"""Small, serializable models used at the application-agent boundary."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.db.models import (
    ApplicationFailure,
    ApplicationStatus,
    CheckpointStage,
    VerificationType,
)


class ApplicationJob(BaseModel):
    """Safe job data returned to Codex for one application."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    company: str
    title: str
    location: str
    apply_url: str
    description: str | None = None
    status: ApplicationStatus


class ApplicationCheckpoint(BaseModel):
    """Current state and resume information, excluding secrets."""

    model_config = ConfigDict(extra="forbid")

    job_id: int
    status: ApplicationStatus
    stage: CheckpointStage | None = None
    current_url: str | None = None
    confirmation_url: str | None = None
    verification_type: VerificationType | None = None
    resume_instruction: str | None = None
    failure_code: ApplicationFailure | None = None
    failure_message: str | None = None
    review_question: str | None = None
    review_reason: str | None = None
    attempt_count: int
    started_at: datetime | None = None
    updated_at: datetime | None = None
    submitted_at: datetime | None = None


def checkpoint_from_attempt(attempt: object) -> ApplicationCheckpoint:
    """Project an ORM attempt to a safe MCP response."""
    status = ApplicationStatus(attempt.status)  # type: ignore[attr-defined]
    needs_verification = status is ApplicationStatus.NEEDS_VERIFICATION
    failed = status is ApplicationStatus.FAILED
    needs_review = status is ApplicationStatus.NEEDS_REVIEW
    raw_stage = getattr(attempt, "checkpoint_stage", None)
    stage = CheckpointStage(raw_stage) if raw_stage is not None else None
    return ApplicationCheckpoint(
        job_id=attempt.job_id,  # type: ignore[attr-defined]
        status=status,
        stage=stage,
        current_url=attempt.current_url,  # type: ignore[attr-defined]
        confirmation_url=attempt.confirmation_url,  # type: ignore[attr-defined]
        verification_type=(
            attempt.verification_type if needs_verification else None  # type: ignore[attr-defined]
        ),
        resume_instruction=(
            attempt.resume_instruction if needs_verification else None  # type: ignore[attr-defined]
        ),
        failure_code=attempt.failure_code if failed else None,  # type: ignore[attr-defined]
        failure_message=attempt.failure_message if failed else None,  # type: ignore[attr-defined]
        review_question=(
            attempt.review_question if needs_review else None  # type: ignore[attr-defined]
        ),
        review_reason=attempt.review_reason if needs_review else None,  # type: ignore[attr-defined]
        attempt_count=attempt.attempt_count,  # type: ignore[attr-defined]
        started_at=attempt.started_at,  # type: ignore[attr-defined]
        updated_at=attempt.updated_at,  # type: ignore[attr-defined]
        submitted_at=attempt.submitted_at,  # type: ignore[attr-defined]
    )


__all__ = [
    "ApplicationCheckpoint",
    "ApplicationFailure",
    "ApplicationJob",
    "ApplicationStatus",
    "CheckpointStage",
    "VerificationType",
    "checkpoint_from_attempt",
]
