"""Validated agent plans and local tracker state."""

from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

Text = Annotated[str, Field(min_length=1, max_length=2000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Provider(StrictModel):
    base_url: HttpUrl = HttpUrl("https://api.openai.com/v1")
    model: Text
    key_env: str = Field(default="JOBPING_TRACKER_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("base_url")
    @classmethod
    def secure_endpoint(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.scheme != "https"
            or value.username
            or value.password
            or value.query
            or value.fragment
        ):
            raise ValueError("Provider URL must be HTTPS without credentials, query, or fragment")
        return value


class Plan(StrictModel):
    name: Text
    instructions: Text
    fields: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]] = Field(
        min_length=1, max_length=12
    )

    @field_validator("fields")
    @classmethod
    def unique_fields(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate tracker fields")
        return value


class JobObservation(StrictModel):
    url: HttpUrl
    title: Text
    company: Text
    status: Literal["open", "closed", "unknown"]
    facts: dict[str, str | None]

    @field_validator("url")
    @classmethod
    def public_link(cls, value: HttpUrl) -> HttpUrl:
        if urlsplit(str(value)).username or value.password:
            raise ValueError("Job links must not contain credentials")
        return value


class Observation(StrictModel):
    complete: Annotated[bool, Field(strict=True)]
    jobs: list[JobObservation] = Field(max_length=200)


class Change(StrictModel):
    kind: Literal["added", "updated", "missing"]
    url: str
    before: JobObservation | None = None
    after: JobObservation | None = None


class Check(StrictModel):
    checked_at: str
    baseline: bool
    jobs: list[JobObservation]
    changes: list[Change]


class Tracker(StrictModel):
    version: Literal[1] = 1
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    url: HttpUrl
    scope: Literal["posting", "company"]
    request: Text
    provider: Provider
    plan: Plan
    interval_seconds: int = Field(default=3600, ge=60, le=604800)
    history: list[Check] = Field(default_factory=list, max_length=100)
