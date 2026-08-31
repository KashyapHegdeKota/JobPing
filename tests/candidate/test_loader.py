import json
from pathlib import Path

import pytest
from app.candidate.loader import CandidateProfileError, load_candidate_profile


def test_load_candidate_profile_resolves_relative_resume(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")
    payload = {
        "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
        "education": {"school": "U", "degree": "BS", "major": "CS"},
        "links": {},
        "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
        "resume_path": "resume.pdf",
    }
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(payload))
    profile = load_candidate_profile(path)
    assert profile.resume_path == resume


def test_missing_resume_is_clear(tmp_path: Path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text("{}")
    with pytest.raises(CandidateProfileError, match="Invalid candidate profile"):
        load_candidate_profile(path)


def test_validation_error_does_not_expose_candidate_contents(tmp_path: Path) -> None:
    payload = candidate_payload()
    payload["personal"] = {
        "first_name": "Private First Name",
        "last_name": "Private Last Name",
        "email": "not-an-email",
    }
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CandidateProfileError) as caught:
        load_candidate_profile(path)

    assert "Private First Name" not in str(caught.value)
    assert "Private Last Name" not in str(caught.value)


def candidate_payload(resume: str = "resume.pdf") -> dict[str, object]:
    return {
        "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
        "education": {"school": "U", "degree": "BS", "major": "CS"},
        "links": {},
        "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
        "resume_path": resume,
    }


@pytest.mark.parametrize("case", ["missing", "invalid_email", "invalid_json"])
def test_loader_rejects_invalid_profiles(tmp_path: Path, case: str) -> None:
    path = tmp_path / "candidate.json"
    if case == "invalid_json":
        path.write_text("not json")
    else:
        payload = candidate_payload()
        if case == "missing":
            payload.pop("personal")
        else:
            payload["personal"] = {"first_name": "J", "last_name": "D", "email": "bad"}
        path.write_text(json.dumps(payload))
    with pytest.raises(CandidateProfileError):
        load_candidate_profile(path)


def test_loader_accepts_absolute_resume(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"pdf")
    path = tmp_path / "candidate.json"
    payload = candidate_payload(str(resume))
    path.write_text(json.dumps(payload))
    assert load_candidate_profile(path).resume_path == resume
