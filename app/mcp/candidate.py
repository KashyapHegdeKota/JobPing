"""Candidate facts and resume-path tools."""

from __future__ import annotations

from app.candidate.models import CandidateProfile


def candidate_get_profile(profile: CandidateProfile) -> dict[str, object]:
    """Return only fields an application agent needs; never include resume contents."""
    return {
        "personal": {
            "first_name": profile.personal.first_name,
            "last_name": profile.personal.last_name,
            "email": str(profile.personal.email),
            "phone": profile.personal.phone,
            "location": profile.personal.location,
        },
        "education": profile.education.model_dump(),
        "links": {
            key: (str(value) if value is not None else None)
            for key, value in profile.links.model_dump().items()
        },
        "work_authorization": profile.work_authorization.model_dump(),
    }


def candidate_get_resume_path(profile: CandidateProfile) -> str:
    """Return the validated local resume path without reading or logging the file."""
    if not profile.resume_path.is_file():
        raise ValueError("candidate resume is no longer available")
    if profile.resume_path.suffix.casefold() != ".pdf":
        raise ValueError("candidate resume must be a PDF")
    return str(profile.resume_path)


def candidate_get_contact(profile: CandidateProfile) -> dict[str, object]:
    """Return only contact fields needed for basic application inputs."""
    return {
        "first_name": profile.personal.first_name,
        "last_name": profile.personal.last_name,
        "email": str(profile.personal.email),
        "phone": profile.personal.phone,
        "location": profile.personal.location,
    }


def candidate_get_education(profile: CandidateProfile) -> dict[str, object]:
    """Return only education facts needed by an application form."""
    return profile.education.model_dump()


def candidate_get_links(profile: CandidateProfile) -> dict[str, object]:
    """Return only candidate-provided public profile links."""
    return {
        key: (str(value) if value is not None else None)
        for key, value in profile.links.model_dump().items()
    }


def candidate_get_work_authorization(profile: CandidateProfile) -> dict[str, object]:
    """Return deterministic legal-work facts; never infer them in the agent."""
    return profile.work_authorization.model_dump()


__all__ = [
    "candidate_get_contact",
    "candidate_get_education",
    "candidate_get_links",
    "candidate_get_profile",
    "candidate_get_resume_path",
    "candidate_get_work_authorization",
]
