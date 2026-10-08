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


@pytest.mark.parametrize(
    ("source", "source_id", "payload", "expected"),
    [
        ("greenhouse", "greenhouse:waymo:123", {"id": 123}, ("greenhouse:waymo", "123")),
        (
            "lever",
            "uuid-123",
            {"provider": "lever", "site": "acme", "raw": {}},
            ("lever:acme", "uuid-123"),
        ),
        ("applyguy_internships", "feed-id", {"site": "acme"}, None),
        ("lever", "uuid-123", {"site": "../acme"}, None),
    ],
)
def test_direct_api_identity_survives_branded_application_urls(
    source: str, source_id: str, payload: dict[str, object], expected: tuple[str, str] | None
) -> None:
    assert (
        stable_posting_identity(
            source=source,
            source_id=source_id,
            apply_url="https://careers.example.com/jobs/title",
            payload=payload,
        )
        == expected
    )
