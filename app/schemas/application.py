"""ATS-independent normalized application form schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class ApplicationFieldType(StrEnum):
    TEXT = "text"
    EMAIL = "email"
    TEL = "tel"
    TEXTAREA = "textarea"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    FILE = "file"
    DATE = "date"
    NUMBER = "number"
    UNKNOWN = "unknown"


class ApplicationOption(BaseModel):
    value: str | None = None
    label: str


class ApplicationField(BaseModel):
    id: str
    label: str
    field_type: ApplicationFieldType
    required: bool = False
    options: list[ApplicationOption] = Field(default_factory=list)
    selector_hint: str | None = None


class ApplicationForm(BaseModel):
    ats: str
    job_id: int
    url: HttpUrl
    fields: list[ApplicationField]
    company: str | None = None
    role: str | None = None
