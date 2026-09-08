from datetime import UTC, datetime

from app.services.posting_dates import parse_source_posted_at


def test_parse_source_posted_at() -> None:
    observed_at = datetime(2026, 9, 8, 14, 25, 42, tzinfo=UTC)

    assert parse_source_posted_at("0d", observed_at=observed_at) == datetime(
        2026, 9, 8, 0, 0, 0, tzinfo=UTC
    )
    assert parse_source_posted_at("1w", observed_at=observed_at) == datetime(
        2026, 9, 1, 0, 0, 0, tzinfo=UTC
    )
    assert parse_source_posted_at("1mo", observed_at=observed_at) == datetime(
        2026, 8, 8, 0, 0, 0, tzinfo=UTC
    )
    assert parse_source_posted_at("1y", observed_at=observed_at) == datetime(
        2025, 9, 8, 0, 0, 0, tzinfo=UTC
    )
    assert parse_source_posted_at("Aug 11", observed_at=observed_at) == datetime(
        2026, 8, 11, 0, 0, 0, tzinfo=UTC
    )
    assert parse_source_posted_at("Oct 11", observed_at=observed_at) == datetime(
        2025, 10, 11, 0, 0, 0, tzinfo=UTC
    )  # Future date is pushed to previous year
    assert parse_source_posted_at("2026-08-11", observed_at=observed_at) == datetime(
        2026, 8, 11, 0, 0, 0, tzinfo=UTC
    )

    assert parse_source_posted_at("invalid", observed_at=observed_at) is None
    assert parse_source_posted_at(None, observed_at=observed_at) is None
