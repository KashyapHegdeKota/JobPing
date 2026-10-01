"""Narrow candidate projections and resume availability at call time."""

import pytest
from app.candidate.models import CandidateProfile
from app.mcp.candidate import (
    candidate_get_contact,
    candidate_get_education,
    candidate_get_links,
    candidate_get_profile,
    candidate_get_resume_path,
    candidate_get_work_authorization,
)


def test_narrow_fact_tools_expose_only_requested_sections(
    candidate_profile: CandidateProfile,
) -> None:
    profile = candidate_get_profile(candidate_profile)
    assert candidate_get_contact(candidate_profile) == profile["personal"]
    assert candidate_get_education(candidate_profile) == profile["education"]
    assert candidate_get_links(candidate_profile) == profile["links"]
    assert candidate_get_work_authorization(candidate_profile) == profile["work_authorization"]
    assert set(profile) == {"personal", "education", "links", "work_authorization"}
    assert "resume_path" not in profile


def test_missing_resume_after_profile_load_is_rejected(candidate_profile: CandidateProfile) -> None:
    assert candidate_get_resume_path(candidate_profile).endswith("resume.pdf")
    candidate_profile.resume_path.unlink()
    with pytest.raises(ValueError, match="no longer available") as error:
        candidate_get_resume_path(candidate_profile)
    assert str(candidate_profile.resume_path) not in str(error.value)


def test_changed_resume_extension_is_rejected(candidate_profile: CandidateProfile) -> None:
    wrong = candidate_profile.resume_path.with_suffix(".txt")
    wrong.write_text("synthetic", encoding="utf-8")
    altered = candidate_profile.model_copy(update={"resume_path": wrong})
    with pytest.raises(ValueError, match="must be a PDF"):
        candidate_get_resume_path(altered)
