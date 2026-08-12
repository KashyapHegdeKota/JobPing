"""Pydantic models at the ingestion and persistence boundaries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class JobType(StrEnum):
    """Job categories accepted by the discovery engine."""

    INTERNSHIP = "internship"
    NEW_GRAD = "new_grad"


class RawJobPayload(BaseModel):
    """A permissive envelope for an unprocessed record from any scraper."""

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=100)
    source_id: str | None = Field(default=None, max_length=500)
    company: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=1000)
    apply_url: str | None = Field(default=None, max_length=4096)
    location: str | list[str] | None = None
    season: int | str | None = None
    job_type: str | None = Field(default=None, max_length=100)
    is_closed: bool | str | int | None = None
    observed_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NormalizedJob(BaseModel):
    """Canonical, database-ready representation of a job posting."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, use_enum_values=True)

    company_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=500)
    base_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    apply_url: HttpUrl
    location: str = Field(min_length=1, max_length=500)
    season: int = Field(ge=2026, le=2027)
    job_type: JobType
    is_closed: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("job_type", mode="before")
    @classmethod
    def normalize_job_type(cls, value: object) -> object:
        """Accept human-readable scraper labels while storing canonical values."""
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
            aliases = {"intern": "internship", "newgrad": "new_grad"}
            return aliases.get(normalized, normalized)
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        """Reject ambiguous timestamps at the persistence boundary."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamp must include a timezone")
        return value
