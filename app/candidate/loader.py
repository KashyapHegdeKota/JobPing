"""Load and validate a private candidate profile without exposing its contents."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.candidate.models import CandidateProfile


class CandidateProfileError(ValueError):
    """An actionable candidate profile configuration error."""


def load_candidate_profile(path: Path) -> CandidateProfile:
    """Load a profile, resolve its resume relative to the profile, and verify it."""
    profile_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CandidateProfileError(f"Candidate profile not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise CandidateProfileError(f"Invalid candidate JSON at {path}: {exc.msg}") from exc
    except OSError as exc:
        raise CandidateProfileError(f"Unable to read candidate profile: {exc}") from exc
    if not isinstance(data, dict):
        raise CandidateProfileError("Candidate profile JSON must be an object")
    resume = data.get("resume_path")
    if isinstance(resume, str):
        resume_path = Path(resume)
        if not resume_path.is_absolute():
            data["resume_path"] = str(profile_path.parent / resume_path)
    try:
        profile = CandidateProfile.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)
        )
        raise CandidateProfileError(f"Invalid candidate profile: {details}") from exc
    if not profile.resume_path.is_file():
        raise CandidateProfileError(f"Candidate resume not found: {profile.resume_path}")
    return profile
