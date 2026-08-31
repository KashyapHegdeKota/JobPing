"""Small, serializable models used at the application-agent boundary."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.db.models import ApplicationFailure, ApplicationStatus, VerificationType


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
    current_url: str | None = None
    confirmation_url: str | None = None
    verification_type: VerificationType | None = None
    resume_instruction: str | None = None
    failure_code: ApplicationFailure | None = None
    failure_message: str | None = None
    attempt_count: int


def checkpoint_from_attempt(attempt: object) -> ApplicationCheckpoint:
    """Project an ORM attempt to a safe MCP response."""
    status = ApplicationStatus(attempt.status)  # type: ignore[attr-defined]
    needs_verification = status is ApplicationStatus.NEEDS_VERIFICATION
    failed = status is ApplicationStatus.FAILED
    return ApplicationCheckpoint(
        job_id=attempt.job_id,  # type: ignore[attr-defined]
        status=status,
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
        attempt_count=attempt.attempt_count,  # type: ignore[attr-defined]
    )


__all__ = [
    "ApplicationCheckpoint",
    "ApplicationFailure",
    "ApplicationJob",
    "ApplicationStatus",
    "VerificationType",
    "checkpoint_from_attempt",
]
