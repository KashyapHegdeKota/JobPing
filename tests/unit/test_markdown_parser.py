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


def test_lock_icon_and_variation_selectors_mark_roles_closed_and_are_cleaned() -> None:
    for lock in ("🔒", "🔒️", "🔒︎"):
        result = parse_markdown_table_row(
            f"| Acme {lock} | Engineer Intern | Remote | "
            "[Apply 🔒](https://example.com/apply) | Today |"
        )

        assert result is not None
        assert result.is_closed is True
        assert result.company == "Acme"
        assert result.apply_url == "https://example.com/apply"


def test_strikethrough_in_relevant_fields_marks_role_closed_and_cleans_text() -> None:
    result = parse_markdown_table_row(
        "| Acme | ~~Software Engineer Intern~~ | ~~Remote~~ | "
        "[~~Apply~~](https://example.com/apply) | Today |"
    )

    assert result is not None
    assert result.is_closed is True
    assert result.title == "Software Engineer Intern"
    assert result.location == "Remote"
    assert result.apply_url == "https://example.com/apply"


def test_standalone_formatted_closed_status_marks_role_closed() -> None:
    result = parse_markdown_table_row(
        "| Acme | Engineer Intern | Remote | [Apply](https://example.com/apply) | "
        "<strong>ClOsEd</strong> |",
        headers=("Company", "Role", "Location", "Apply", "Status"),
    )

    assert result is not None
    assert result.is_closed is True


def test_combined_closed_signals_are_supported() -> None:
    result = parse_markdown_table_row(
        "| 🔒 Acme | ~~Engineer Intern~~ | Remote | "
        "[Apply](https://example.com/apply) | **Closed** |",
        headers=("Company", "Role", "Location", "Apply", "Status"),
    )

    assert result is not None
    assert result.is_closed is True
    assert result.company == "Acme"
    assert result.title == "Engineer Intern"


def test_open_and_reopened_rows_remain_open() -> None:
    for status in ("Open", "Reopened"):
        result = parse_markdown_table_row(
            f"| Acme | Engineer Intern | Remote | [Apply](https://example.com/apply) | {status} |",
            headers=("Company", "Role", "Location", "Apply", "Status"),
        )

        assert result is not None
        assert result.is_closed is False


def test_closed_word_substrings_and_urls_do_not_create_false_positives() -> None:
    result = parse_markdown_table_row(
        "| Closed-Loop Systems | Closed Caption Engineer | Remote | "
        "[Apply](https://example.com/apply?team=closed&mode=closed-loop) | Today |"
    )

    assert result is not None
    assert result.is_closed is False
    assert result.company == "Closed-Loop Systems"
    assert result.title == "Closed Caption Engineer"

    url_markup = parse_markdown_table_row(
        "| Acme | Engineer Intern | Remote | "
        "[Apply](https://example.com/apply?decorator=~~preview~~&status=closed) | Today |"
    )
    assert url_markup is not None
    assert url_markup.is_closed is False
