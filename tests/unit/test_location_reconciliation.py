"""Deterministic location authority behavior."""

from __future__ import annotations

from app.schemas.job import NormalizedJob
from app.services.hasher import generate_content_hash
from app.services.location_reconciliation import reconcile_location


def job(location: str, source: str | None) -> NormalizedJob:
    base_hash = "a" * 64
    url = "https://example.com/jobs/1"
    return NormalizedJob(
        company_name="Example",
        title="Engineer",
        base_hash=base_hash,
        content_hash=generate_content_hash(base_hash, url, location, False),
        apply_url=url,
        location=location,
        location_source=source,
        season=2027,
        job_type="internship",
    )


def reconcile(incoming: NormalizedJob, existing: NormalizedJob) -> NormalizedJob:
    return reconcile_location(
        incoming,
        existing_location=existing.location,
        existing_source=existing.location_source,
    )


def test_same_source_can_replace_a_distinct_city() -> None:
    result = reconcile(job("Seattle, WA", "greenhouse"), job("Austin, TX", "greenhouse"))

    assert result.location == "Seattle, WA"
    assert result.location_source == "greenhouse"
    assert result.content_hash == job("Seattle, WA", "greenhouse").content_hash


def test_tier_and_equal_tier_tie_break_are_order_independent() -> None:
    weak = job("Remote", "applyguy_internships")
    strong = job("Remote, U.S.", "greenhouse")
    assert reconcile(strong, weak).location == "Remote, U.S."
    assert reconcile(weak, strong).location == "Remote, U.S."

    greenhouse = job("Austin, TX", "greenhouse")
    lever = job("Seattle, WA", "lever")
    assert reconcile(greenhouse, lever).location == "Austin, TX"
    assert reconcile(lever, greenhouse).location == "Austin, TX"


def test_browser_scraper_source_labels_are_high_authority() -> None:
    applyguy = job("Remote", "applyguy_internships")
    for source in ("amazon_jobs", "meta_careers", "workday"):
        assert reconcile(job("Remote, U.S.", source), applyguy).location == "Remote, U.S."


def test_unknown_and_legacy_provenance_are_conservative_but_compatible() -> None:
    assert reconcile(job("Seattle, WA", "custom_b"), job("Austin, TX", "custom_a")).location == (
        "Austin, TX"
    )
    # Existing callers with no provenance retain their old last-write behavior.
    assert reconcile(job("Seattle, WA", None), job("Austin, TX", None)).location == "Seattle, WA"
    # A new unknown source cannot implicitly take over a legacy row.
    assert reconcile(job("Seattle, WA", "custom_a"), job("Austin, TX", None)).location == (
        "Austin, TX"
    )
    # A recognized source can upgrade the unknown legacy row once.
    assert reconcile(job("Seattle, WA", "greenhouse"), job("Austin, TX", None)).location == (
        "Seattle, WA"
    )


def test_placeholders_do_not_erase_or_block_meaningful_locations() -> None:
    existing = job("Remote", "applyguy_internships")
    assert reconcile(job("Unspecified", "greenhouse"), existing).location == "Remote"
    assert reconcile(job("Seattle, WA", "greenhouse"), job("Unspecified", None)).location == (
        "Seattle, WA"
    )
