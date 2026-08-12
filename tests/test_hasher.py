"""Tests for deterministic job identity and content hashing."""

import pytest
from app.services.hasher import generate_base_hash, generate_content_hash


def test_content_hash_normalizes_equivalent_inputs() -> None:
    base_hash = generate_base_hash("Example", "Software Engineer Intern")

    first = generate_content_hash(
        base_hash.upper(),
        " HTTPS://JOBS.EXAMPLE.COM/apply?id=42 ",
        " New York,\t NY ",
        False,
    )
    second = generate_content_hash(
        base_hash,
        "https://jobs.example.com/apply?id=42",
        "new york, ny",
        False,
    )

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    ("changed_url", "changed_location", "is_closed"),
    [
        ("https://jobs.example.com/apply?id=43", "New York, NY", False),
        ("https://jobs.example.com/Apply?id=42", "New York, NY", False),
        ("https://jobs.example.com/apply?id=42", "Boston, MA", False),
        ("https://jobs.example.com/apply?id=42", "New York, NY", True),
    ],
)
def test_content_hash_changes_for_material_state_changes(
    changed_url: str, changed_location: str, is_closed: bool
) -> None:
    base_hash = "a" * 64
    original = generate_content_hash(
        base_hash, "https://jobs.example.com/apply?id=42", "New York, NY", False
    )

    assert generate_content_hash(base_hash, changed_url, changed_location, is_closed) != original


@pytest.mark.parametrize("base_hash", ["", "a" * 63, "g" * 64, "sha256:" + "a" * 64])
def test_content_hash_rejects_invalid_base_hash(base_hash: str) -> None:
    with pytest.raises(ValueError, match="64-character hexadecimal"):
        generate_content_hash(base_hash, "https://example.com/apply", "Remote", False)
