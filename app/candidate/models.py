"""Pydantic models for locally stored candidate information."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, EmailStr, HttpUrl


class PersonalInfo(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone: str | None = None
    location: str | None = None


class EducationInfo(BaseModel):
    school: str
    degree: str
    major: str
    graduation_date: str | None = None
    gpa: str | None = None


class CandidateLinks(BaseModel):
    linkedin: HttpUrl | None = None
    github: HttpUrl | None = None
    portfolio: HttpUrl | None = None


class WorkAuthorization(BaseModel):
    authorized_us: bool
    requires_sponsorship: bool


class CandidateProfile(BaseModel):
    personal: PersonalInfo
    education: EducationInfo
    links: CandidateLinks
    work_authorization: WorkAuthorization
    resume_path: Path
