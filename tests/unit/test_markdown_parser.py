"""Tests for Simplify-style Markdown table row parsing."""

from app.scrapers.markdown_parser import parse_markdown_table_row


def test_parses_canonical_row_and_preserves_source_data() -> None:
    row = (
        "| **Acme &amp; Co.** | `Software Engineer Intern` | New York<br>Remote | "
        "[Apply](https://jobs.example.com/apply?id=1&src=gh) | Aug 11 |"
    )
    result = parse_markdown_table_row(row, source_id="README.md:42")

    assert result is not None
    assert result.company == "Acme & Co."
    assert result.title == "Software Engineer Intern"
    assert result.location == "New York; Remote"
    assert result.apply_url == "https://jobs.example.com/apply?id=1&src=gh"
    assert result.source_id == "README.md:42"
    assert result.payload["date_posted"] == "Aug 11"
    assert result.payload["raw_row"] == row


def test_handles_escaped_pipe_and_balanced_parentheses_in_url() -> None:
    result = parse_markdown_table_row(
        r"| Foo \| Labs | R&D Intern | Austin, TX | "
        r"[Apply](https://example.com/jobs/(student)?ref=a(b)) | 2d |"
    )

    assert result is not None
    assert result.company == "Foo | Labs"
    assert result.apply_url == "https://example.com/jobs/(student)?ref=a(b)"


def test_accepts_reordered_layout_and_bare_url() -> None:
    result = parse_markdown_table_row(
        "| 2026-08-11 | Seattle | https://example.com/apply/123 | "
        "New Grad Engineer | Example | Remote |",
        headers=("Date Posted", "Location", "Link", "Position", "Employer", "Notes"),
    )

    assert result is not None
    assert result.company == "Example"
    assert result.title == "New Grad Engineer"
    assert result.location == "Seattle"
    assert result.apply_url == "https://example.com/apply/123"


def test_extracts_url_from_badge_image_link() -> None:
    result = parse_markdown_table_row(
        "| Acme | Intern | Remote | "
        "[![Apply](badge.svg)](https://acme.test/apply?q=intern) | Today |"
    )

    assert result is not None
    assert result.apply_url == "https://acme.test/apply?q=intern"


def test_skips_headers_separators_and_malformed_text() -> None:
    assert (
        parse_markdown_table_row("| Company | Role | Location | Application Link | Date |") is None
    )
    assert parse_markdown_table_row("| :--- | --- | ---: | --- | --- |") is None
    assert parse_markdown_table_row("This is ordinary prose") is None
    assert parse_markdown_table_row("| Acme | Intern | Remote | unavailable | Today |") is None
