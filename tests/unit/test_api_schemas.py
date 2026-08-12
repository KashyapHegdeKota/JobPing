"""Tests for public API response schemas."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.schemas.api import (
    CompanyResponse,
    JobResponse,
    PaginatedJobResponse,
    StatusLogResponse,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def company() -> CompanyResponse:
    return CompanyResponse(id=1, name="Example", domain="example.com", created_at=NOW)


def job() -> JobResponse:
    return JobResponse(
        id=2,
        company_id=1,
        company=company(),
        title="Software Engineer Intern",
        apply_url="https://example.com/jobs/2",
        location="New York, NY",
        season=2027,
        job_type="internship",
        is_closed=False,
        created_at=NOW,
        updated_at=NOW,
    )


def test_job_response_serializes_nested_company_and_url() -> None:
    payload = job().model_dump(mode="json")

    assert payload["company"]["name"] == "Example"
    assert payload["apply_url"] == "https://example.com/jobs/2"
    assert payload["job_type"] == "internship"


def test_response_schemas_validate_orm_shaped_objects() -> None:
    orm_company = SimpleNamespace(id=1, name="Example", domain=None, created_at=NOW)
    orm_job = SimpleNamespace(
        id=2,
        company_id=1,
        company=orm_company,
        title="New Grad Engineer",
        apply_url="https://example.com/jobs/2",
        location="Remote",
        season=2027,
        job_type="new_grad",
        is_closed=False,
        created_at=NOW,
        updated_at=NOW,
    )

    response = JobResponse.model_validate(orm_job)

    assert response.company.domain is None
    assert response.job_type == "new_grad"


def test_paginated_response_computes_metadata() -> None:
    response = PaginatedJobResponse(items=[job()], total=41, page=2, page_size=20)

    assert response.total_pages == 3
    assert response.has_next is True
    assert response.has_previous is True
    assert response.model_dump()["total_pages"] == 3


def test_empty_page_has_no_navigation() -> None:
    response = PaginatedJobResponse(total=0)

    assert response.total_pages == 0
    assert response.has_next is False
    assert response.has_previous is False


def test_status_log_response_accepts_initial_transition() -> None:
    response = StatusLogResponse(
        id=3,
        job_id=2,
        previous_state=None,
        new_state="OPEN",
        changed_at=NOW,
    )

    assert response.previous_state is None


def test_pagination_rejects_invalid_page_size() -> None:
    with pytest.raises(ValidationError):
        PaginatedJobResponse(total=1, page_size=101)
