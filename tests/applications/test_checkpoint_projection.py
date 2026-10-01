"""Stale checkpoint metadata must not leak across status boundaries."""

from types import SimpleNamespace

import pytest
from app.applications.models import ApplicationCheckpoint, checkpoint_from_attempt
from app.db.models import ApplicationStatus
from pydantic import ValidationError


@pytest.mark.parametrize("status", list(ApplicationStatus))
def test_projection_exposes_only_current_status_metadata(status: ApplicationStatus) -> None:
    attempt = SimpleNamespace(
        job_id=1,
        status=status,
        checkpoint_stage=None,
        current_url=None,
        confirmation_url=None,
        verification_type="captcha",
        resume_instruction="Complete challenge in browser.",
        failure_code="browser_error",
        failure_message="Browser unavailable.",
        review_question="Unknown question",
        review_reason="Missing fact",
        attempt_count=1,
        started_at=None,
        updated_at=None,
        submitted_at=None,
    )
    projected = checkpoint_from_attempt(attempt).model_dump(mode="json")
    assert (projected["verification_type"] is not None) == (
        status == ApplicationStatus.NEEDS_VERIFICATION
    )
    assert (projected["resume_instruction"] is not None) == (
        status == ApplicationStatus.NEEDS_VERIFICATION
    )
    assert (projected["failure_code"] is not None) == (status == ApplicationStatus.FAILED)
    assert (projected["review_question"] is not None) == (status == ApplicationStatus.NEEDS_REVIEW)
    assert (projected["review_reason"] is not None) == (status == ApplicationStatus.NEEDS_REVIEW)


@pytest.mark.parametrize("secret_field", ["otp", "password", "cookie", "token"])
def test_checkpoint_schema_forbids_secret_fields(secret_field: str) -> None:
    with pytest.raises(ValidationError):
        ApplicationCheckpoint.model_validate(
            {
                "job_id": 1,
                "status": "needs_verification",
                "attempt_count": 1,
                secret_field: "sensitive",
            }
        )
