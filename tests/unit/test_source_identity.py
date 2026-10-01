"""Conservative source identity extraction tests."""

from __future__ import annotations

import pytest
from app.services.source_identity import stable_posting_identity


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Staff-Software-Engineer_R-2580206",
            ("workday:acme.wd5.myworkdayjobs.com", "R-2580206"),
        ),
        (
            "https://acme.myworkdayjobs.com/jobs/Software-Engineer_JR12345?source=career_site",
            ("workday:acme.myworkdayjobs.com", "JR12345"),
        ),
        (
            "https://acme.myworkdayjobs.com/en-US/job/R-12345",
            ("workday:acme.myworkdayjobs.com", "R-12345"),
        ),
        (
            "https://acme.myworkdayjobs.com/en-US/job/JR12345",
            ("workday:acme.myworkdayjobs.com", "JR12345"),
        ),
        ("https://acme.myworkdayjobs.com/en-US/job/Staff-Engineer", None),
        ("https://acme.myworkdayjobs.com/en-US/job/Staff-Engineer_R-12345-extra", None),
        ("https://acme.myworkdayjobs.com/en-US/job/", None),
    ],
)
def test_workday_requisition_identity_is_extracted_conservatively(
    url: str, expected: tuple[str, str] | None
) -> None:
    assert (
        stable_posting_identity(source="workday", source_id=None, apply_url=url, payload={})
        == expected
    )


def test_workday_title_slug_changes_are_not_authoritative_ids() -> None:
    identities = [
        stable_posting_identity(
            source="workday",
            source_id=None,
            apply_url=f"https://acme.myworkdayjobs.com/en-US/job/{slug}",
            payload={},
        )
        for slug in ("Staff-Engineer", "Senior-Staff-Engineer")
    ]
    assert identities == [None, None]
