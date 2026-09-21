"""Unit tests for deterministic job identity and content hashing."""

import pytest
from app.services.hasher import (
    canonicalize_apply_url,
    choose_canonical_apply_url,
    generate_base_hash,
    generate_content_hash,
)


def test_base_hash_has_exact_deterministic_digest() -> None:
    assert generate_base_hash("OpenAI", "Software Engineer Intern") == (
        "c7a98cc595edbe4e59da82f1c415936e31ec5707768837f8af210debeddd7dfd"
    )


def test_base_hash_normalizes_case_unicode_punctuation_and_whitespace() -> None:
    canonical = generate_base_hash("Acme Corp", "Software Engineer Intern")

    assert generate_base_hash("  ACME—CORP ", "software_engineer\tINTERN") == canonical
    assert generate_base_hash("Ａｃｍｅ Corp", "Software Engineer Intern") == canonical


def test_base_hash_length_prefixes_resist_delimiter_ambiguity() -> None:
    assert generate_base_hash("ab", "c") != generate_base_hash("a", "bc")


def test_content_hash_has_exact_deterministic_digest() -> None:
    assert (
        generate_content_hash(
            "a" * 64,
            "https://jobs.example.com/apply?id=42",
            "New York, NY",
            False,
        )
        == "d7154bb120e28b95a147b2d69cbf34dc03f51223b3b97876cfa0dfa028e29baa"
    )


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


@pytest.mark.parametrize(
    ("changed_base", "changed_url", "changed_location", "is_closed"),
    [
        ("b" * 64, "https://jobs.example.com/apply?id=42", "New York, NY", False),
        ("a" * 64, "https://jobs.example.com/apply?id=43", "New York, NY", False),
        ("a" * 64, "https://jobs.example.com/Apply?id=42", "New York, NY", False),
        ("a" * 64, "https://jobs.example.com/apply?id=42", "Boston, MA", False),
        ("a" * 64, "https://jobs.example.com/apply?id=42", "New York, NY", True),
    ],
)
def test_content_hash_changes_for_every_state_field(
    changed_base: str, changed_url: str, changed_location: str, is_closed: bool
) -> None:
    original = generate_content_hash(
        "a" * 64, "https://jobs.example.com/apply?id=42", "New York, NY", False
    )

    assert generate_content_hash(changed_base, changed_url, changed_location, is_closed) != original


def test_content_hash_length_prefixes_resist_field_boundary_ambiguity() -> None:
    first = generate_content_hash("a" * 64, "https://example.com/ab", "c", False)
    second = generate_content_hash("a" * 64, "https://example.com/a", "bc", False)

    assert first != second


@pytest.mark.parametrize("base_hash", ["", "a" * 63, "g" * 64, "sha256:" + "a" * 64])
def test_content_hash_rejects_invalid_base_hash(base_hash: str) -> None:
    with pytest.raises(ValueError, match="64-character hexadecimal"):
        generate_content_hash(base_hash, "https://example.com/apply", "Remote", False)


def test_url_canonicalization_strips_tracking_and_folds_ats_aliases() -> None:
    assert (
        canonicalize_apply_url("https://boards.greenhouse.io/acme/jobs/42?utm_source=simplify")
        == "https://job-boards.greenhouse.io/acme/jobs/42"
    )
    assert canonicalize_apply_url("https://jobs.lever.co/acme/abc?fbclid=1") == (
        "https://jobs.lever.co/acme/abc"
    )


def test_url_canonicalization_preserves_greenhouse_job_id_query() -> None:
    assert canonicalize_apply_url("https://boards.greenhouse.io/acme?gh_jid=42") == (
        "https://job-boards.greenhouse.io/acme?gh_jid=42"
    )


def test_direct_ats_url_wins_over_applyguy_redirect() -> None:
    direct = "https://job-boards.greenhouse.io/acme/jobs/42"
    redirect = "https://applyguy.ai/jobs?id=42"
    assert choose_canonical_apply_url(redirect, direct) == direct
    assert choose_canonical_apply_url(direct, redirect) == direct
