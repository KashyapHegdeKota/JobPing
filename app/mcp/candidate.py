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
    return str(profile.resume_path)


__all__ = ["candidate_get_profile", "candidate_get_resume_path"]
