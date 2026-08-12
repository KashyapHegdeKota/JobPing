"""Validation tests for ingestion and normalized job schemas."""

from datetime import UTC, datetime

import pytest
from app.schemas.job import JobType, NormalizedJob, RawJobPayload
from pydantic import ValidationError


def normalized_values() -> dict[str, object]:
    """Return a valid normalized job payload."""
    return {
        "company_id": 1,
        "title": " Software Engineer Intern ",
        "base_hash": "a" * 64,
        "content_hash": "b" * 64,
        "apply_url": "https://example.com/jobs/123",
        "location": " New York, NY ",
        "season": 2026,
        "job_type": "Internship",
        "created_at": datetime(2026, 8, 11, tzinfo=UTC),
    }


def test_raw_payload_preserves_heterogeneous_source_data() -> None:
    raw = RawJobPayload.model_validate(
        {
            "source": " greenhouse ",
            "location": ["New York", "Remote"],
            "is_closed": "false",
            "payload": {"departments": [{"id": 7}]},
            "vendor_specific": {"requisition": "ABC-1"},
        }
    )

    assert raw.source == "greenhouse"
    assert raw.location == ["New York", "Remote"]
    assert raw.model_extra == {"vendor_specific": {"requisition": "ABC-1"}}


def test_normalized_job_normalizes_labels_and_serializes_json() -> None:
    job = NormalizedJob.model_validate(normalized_values())

    assert job.title == "Software Engineer Intern"
    assert job.job_type == JobType.INTERNSHIP
    assert job.model_dump(mode="json")["apply_url"] == "https://example.com/jobs/123"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("season", 2028),
        ("job_type", "contract"),
        ("base_hash", "not-a-sha256"),
        ("apply_url", "javascript:alert(1)"),
        ("created_at", datetime(2026, 8, 11)),
    ],
)
def test_normalized_job_rejects_invalid_values(field: str, value: object) -> None:
    values = normalized_values()
    values[field] = value

    with pytest.raises(ValidationError):
        NormalizedJob.model_validate(values)


def test_normalized_job_forbids_unknown_fields() -> None:
    values = normalized_values()
    values["source"] = "greenhouse"

    with pytest.raises(ValidationError):
        NormalizedJob.model_validate(values)
