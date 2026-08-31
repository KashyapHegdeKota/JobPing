"""Tests for the standalone Simplify history backfill helpers."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from pytest import MonkeyPatch
from scripts import backfill_simplify_dates


def test_parse_git_timestamp_uses_first_reverse_log_result() -> None:
    result = backfill_simplify_dates._parse_git_timestamp(
        "2026-01-02T03:04:05-05:00\n2026-02-03T04:05:06-05:00\n"
    )

    assert result == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(-timedelta(hours=5)))


def test_collect_earliest_url_timestamps_uses_one_patch_history_command(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_git(arguments: tuple[str, ...], *, cwd: Path | None = None) -> str:
        captured.update(arguments=arguments, cwd=cwd)
        return (
            "Date: 2026-04-05T06:07:08Z\n"
            "+| Example | Role | [Apply](https://example.com/jobs/1?a=b) |\n"
        )

    monkeypatch.setattr(backfill_simplify_dates, "_run_git", fake_run_git)

    result = backfill_simplify_dates.collect_earliest_url_timestamps(
        Path("repository"), {"https://example.com/jobs/1?a=b"}
    )

    assert result == {"https://example.com/jobs/1?a=b": datetime(2026, 4, 5, 6, 7, 8, tzinfo=UTC)}
    assert captured["cwd"] == Path("repository")
    arguments = captured["arguments"]
    assert isinstance(arguments, tuple)
    assert "--reverse" in arguments
    assert "-p" in arguments
    assert "-U0" in arguments
    assert "README*.md" in arguments
    assert "-S" not in arguments


def test_parse_patch_history_keeps_first_addition_and_ignores_removed_urls() -> None:
    url = "https://example.com/jobs/first"
    result = backfill_simplify_dates.parse_patch_history(
        "\n".join(
            (
                "Date: 2026-01-01T10:00:00Z",
                f"-{url}",
                f"+| [Apply]({url}) |",
                "Date: 2026-02-01T10:00:00Z",
                f"+| [Apply]({url}) |",
            )
        ),
        {url},
    )

    assert result[url] == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_current_cycle_repository_defaults_cover_intern_and_new_grad() -> None:
    assert backfill_simplify_dates.DEFAULT_REPOSITORY_URLS == (
        "https://github.com/SimplifyJobs/Summer2027-Internships.git",
        "https://github.com/SimplifyJobs/New-Grad-Positions.git",
    )
