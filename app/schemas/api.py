"""Response data-transfer objects exposed by the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field

from app.schemas.job import JobType


class APIResponse(BaseModel):
    """Shared configuration for ORM-backed API responses."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class CompanyResponse(APIResponse):
    """Public company representation."""

    id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    domain: str | None = Field(default=None, max_length=255)
    created_at: datetime


class JobResponse(APIResponse):
    """A normalized job posting returned to API clients."""

    id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    company: CompanyResponse
    title: str = Field(min_length=1, max_length=500)
    apply_url: HttpUrl
    location: str = Field(min_length=1, max_length=500)
    season: int = Field(ge=2020, le=2100)
    job_type: JobType
    is_closed: bool
    created_at: datetime
    updated_at: datetime


class StatusLogResponse(APIResponse):
    """A durable job-state transition."""

    id: int = Field(gt=0)
    job_id: int = Field(gt=0)
    previous_state: str | None = Field(default=None, max_length=32)
    new_state: str = Field(min_length=1, max_length=32)
    changed_at: datetime


class PaginatedJobResponse(APIResponse):
    """A page of jobs plus stable pagination metadata."""

    items: list[JobResponse] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @computed_field
    @property
    def total_pages(self) -> int:
        """Return zero pages for an empty result, otherwise ceiling(total/page_size)."""
        return (self.total + self.page_size - 1) // self.page_size

    @computed_field
    @property
    def has_next(self) -> bool:
        """Whether another page exists after this page."""
        return self.page < self.total_pages

    @computed_field
    @property
    def has_previous(self) -> bool:
        """Whether a previous page exists."""
        return self.page > 1 and self.total_pages > 0
