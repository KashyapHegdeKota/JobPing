"""Tests for the local Simplify ingestion command."""

from __future__ import annotations

from app import cli
from app.pipelines.simplify_pipeline import PipelineResult
from app.schemas.job import JobType
from pytest import MonkeyPatch
from typer.testing import CliRunner

runner = CliRunner()


def test_run_simplify_parser_passes_options_and_prints_summary(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_process(**kwargs: object) -> tuple[PipelineResult, ...]:
        captured.update(kwargs)
        return (PipelineResult(commit_sha="abc123"),)

    monkeypatch.setattr(cli, "_process_commits", fake_process)
    result = runner.invoke(
        cli.app,
        [
            "run-simplify-parser",
            "--owner",
            "Example",
            "--repo",
            "Jobs",
            "--ref",
            "deadbeef",
            "--target-readme",
            "jobs/README.md",
            "--season",
            "2027",
            "--job-type",
            "new_grad",
            "--redis-url",
            "redis://cache:6379/2",
        ],
    )

    assert result.exit_code == 0
    assert "Processed commit abc123" in result.stdout
    assert "NEW_ROLE=0" in result.stdout
    assert "rejected=0" in result.stdout
    assert captured == {
        "owner": "Example",
        "repo": "Jobs",
        "commit_sha": "deadbeef",
        "limit": 1,
        "target_readme": "jobs/README.md",
        "season": 2027,
        "job_type": JobType.NEW_GRAD,
        "redis_url": "redis://cache:6379/2",
        "github_token": None,
        "full_sync": False,
        "database_url": None,
    }


def test_run_simplify_parser_uses_environment_without_exposing_token(
    monkeypatch: MonkeyPatch,
) -> None:
    async def fake_process(**kwargs: object) -> tuple[PipelineResult, ...]:
        assert kwargs["github_token"] == "top-secret"
        return (PipelineResult(commit_sha="from-env"),)

    monkeypatch.setattr(cli, "_process_commits", fake_process)
    result = runner.invoke(
        cli.app,
        ["run-simplify-parser"],
        env={"GITHUB_TOKEN": "top-secret", "GITHUB_OWNER": "EnvOwner"},
    )

    assert result.exit_code == 0
    assert "from-env" in result.stdout
    assert "top-secret" not in result.output


def test_run_simplify_parser_returns_nonzero_on_failure(monkeypatch: MonkeyPatch) -> None:
    async def fake_process(**kwargs: object) -> tuple[PipelineResult, ...]:
        del kwargs
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(cli, "_process_commits", fake_process)
    result = runner.invoke(cli.app, ["run-simplify-parser"])

    assert result.exit_code == 1
    assert "Simplify parser failed: RuntimeError: service unavailable" in result.output


def test_run_simplify_parser_validates_season_before_execution() -> None:
    result = runner.invoke(cli.app, ["run-simplify-parser", "--season", "2025"])

    assert result.exit_code == 2
    assert "2026" in result.output


def test_audit_db_requires_database_url(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = runner.invoke(cli.app, ["audit-db"])
    assert result.exit_code == 2
    assert "DATABASE_URL is required" in result.output
