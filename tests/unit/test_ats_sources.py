"""Network-free registry, eligibility and direct ATS CLI contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app import cli
from app.pipelines.ats_pipeline import ATSPipeline, ATSPipelineResult, ATSScraperFailure
from app.pipelines.direct_ats_pipeline import DirectATSRun
from app.schemas.job import JobType, RawJobPayload
from app.scrapers.ats_sources import ATSSource, load_ats_sources
from typer.testing import CliRunner


def source_data(**updates: object) -> dict[str, object]:
    return {"provider": "greenhouse", "token": "acme", "company": "Acme", "season": 2027} | updates


def registry(tmp_path: Path, **updates: object) -> Path:
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({"sources": [source_data(**updates)]}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("title", "allow_undated", "expected"),
    [
        ("Software Engineer Intern 2027", False, JobType.INTERNSHIP),
        ("2027 Summer Internship", False, JobType.INTERNSHIP),
        ("Co-op Engineer — 2027", False, JobType.INTERNSHIP),
        ("Software Engineer, New Grad 2027", False, JobType.NEW_GRAD),
        ("Recent Graduate Engineer 2027", False, JobType.NEW_GRAD),
        ("University Graduate - 2027", False, JobType.NEW_GRAD),
        ("College Grad Analyst 2027", False, JobType.NEW_GRAD),
        ("Graduate Developer Programme 2027", False, JobType.NEW_GRAD),
        ("Product Manager Intern 2027", False, JobType.INTERNSHIP),
        ("Software Engineer Intern", False, None),
        ("Software Engineer Intern", True, JobType.INTERNSHIP),
        ("Software Engineer New Grad", True, JobType.NEW_GRAD),
        ("Software Engineer Intern 2026", True, None),
        ("Software Engineer Intern 2028", True, None),
        ("Software Engineer Intern 2026/2027", False, None),
        ("Senior Software Engineer Intern 2027", False, None),
        ("Staff New Grad Engineer 2027", False, None),
        ("Sr. New Grad Engineer 2027", False, None),
        ("Director Graduate Program 2027", False, None),
        ("Software Engineer 2027", True, None),
        ("Entry Level Software Engineer 2027", True, None),
        ("International Software Engineer 2027", True, None),
        ("Graduate Admissions Officer 2027", True, None),
        ("Intern or New Grad 2027", True, None),
        ("Engineer Intern 120270", False, None),
    ],
)
def test_conservative_title_eligibility(
    title: str, allow_undated: bool, expected: JobType | None
) -> None:
    source = ATSSource.model_validate(source_data(allow_undated=allow_undated))
    assert source.eligible_type(title) is expected


def test_category_and_season_are_explicit() -> None:
    source = ATSSource.model_validate(source_data(season=2026, job_types=["new_grad"]))
    assert source.eligible_type("Software Intern 2026") is None
    assert source.eligible_type("New Grad Engineer 2026") is JobType.NEW_GRAD
    assert source.eligible_type("New Grad Engineer 2027") is None


@pytest.mark.parametrize(
    "updates",
    [
        {"provider": "workday"},
        {"token": "https://jobs.lever.co/acme"},
        {"token": "../acme"},
        {"token": "acme?mode=json"},
        {"token": ""},
        {"company": " "},
        {"season": 2028},
        {"job_types": []},
        {"job_types": ["internship", "internship"]},
        {"job_types": ["full_time"]},
        {"allow_undated": "true"},
        {"api_key": "do-not-echo-this"},
    ],
)
def test_invalid_registry_rejected_without_echoing_values(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="Invalid ATS sources file") as error:
        load_ats_sources(registry(tmp_path, **updates))
    assert "do-not-echo-this" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        '{"sources": []}',
        '{"sources": [], "extra": true}',
        json.dumps({"sources": [source_data(), source_data(token="ACME")]}),
    ],
)
def test_invalid_envelope_and_duplicate_board(tmp_path: Path, body: str) -> None:
    path = tmp_path / "sources.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ATS sources file"):
        load_ats_sources(path)


def test_missing_oversized_and_optional_registry(tmp_path: Path) -> None:
    assert load_ats_sources(None) == ()
    with pytest.raises(ValueError, match="Cannot read"):
        load_ats_sources(tmp_path / "missing.json")
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 1_048_577)
    with pytest.raises(ValueError, match="exceeds"):
        load_ats_sources(path)


def test_cli_dry_run_validates_env_registry_without_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected(**kwargs: object) -> None:
        pytest.fail("dry-run must not create clients")

    monkeypatch.setattr(cli, "_process_ats_sync", unexpected)
    result = CliRunner().invoke(
        cli.app,
        ["run-ats-sync", "--dry-run"],
        env={"ATS_SOURCES_FILE": str(registry(tmp_path)), "DATABASE_URL": ""},
    )
    assert result.exit_code == 0
    assert "greenhouse/acme" in result.output
    assert "season=2027" in result.output
    assert "allow_undated=false" in result.output


def test_cli_requires_config_and_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATS_SOURCES_FILE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    runner = CliRunner()
    assert runner.invoke(cli.app, ["run-ats-sync"]).exit_code == 2
    result = runner.invoke(cli.app, ["run-ats-sync", "--sources-file", str(registry(tmp_path))])
    assert result.exit_code == 2
    assert "DATABASE_URL is required" in result.output


@pytest.mark.parametrize("failed", [False, True])
def test_cli_reports_outcomes_and_partial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    captured: dict[str, object] = {}

    async def process(**kwargs: object) -> tuple[DirectATSRun, ...]:
        captured.update(kwargs)
        result = ATSPipelineResult()
        if failed:
            result.failures.append(
                ATSScraperFailure("greenhouse", "Acme", "GreenhouseError", "down")
            )
        return (DirectATSRun(kwargs["sources"][0], 5, 5, result),)

    monkeypatch.setattr(cli, "_process_ats_sync", process)
    result = CliRunner().invoke(
        cli.app,
        [
            "run-ats-sync",
            "--sources-file",
            str(registry(tmp_path)),
            "--database-url",
            "sqlite+aiosqlite://",
        ],
    )
    assert result.exit_code == (1 if failed else 0)
    assert "fetched=5 filtered=5" in result.output
    assert "ROLE_REPOSTED=0" in result.output
    assert captured["sources"][0].company == "Acme"


@pytest.mark.parametrize("error", [RuntimeError("write failed"), asyncio.CancelledError()])
def test_cli_runtime_failure_and_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    async def process(**kwargs: object) -> tuple[DirectATSRun, ...]:
        raise error

    monkeypatch.setattr(cli, "_process_ats_sync", process)
    result = CliRunner().invoke(
        cli.app,
        [
            "run-ats-sync",
            "--sources-file",
            str(registry(tmp_path)),
            "--database-url",
            "sqlite+aiosqlite://",
        ],
    )
    assert result.exit_code == (130 if isinstance(error, asyncio.CancelledError) else 1)


def test_scheduler_has_no_placeholders_and_requires_database() -> None:
    kwargs = {"redis_url": "unused", "github_token": None, "database_url": None}
    assert cli._scheduler_targets(["boards.greenhouse.io=120", "api.lever.co=120"], **kwargs) == ()
    with pytest.raises(ValueError, match="Unsupported scheduler domain"):
        cli._scheduler_targets(["unsupported.example=120"], **kwargs)
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        cli._scheduler_targets(
            ["boards.greenhouse.io=120"],
            ats_sources=(ATSSource.model_validate(source_data()),),
            **kwargs,
        )


def test_scheduler_dry_run_lists_real_targets_and_disabled_boards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ATS_SOURCES_FILE", raising=False)
    runner = CliRunner()
    disabled = runner.invoke(cli.app, ["start-scheduler", "--dry-run"])
    assert disabled.exit_code == 0
    assert "greenhouse: disabled" in disabled.output
    assert "lever: disabled" in disabled.output
    assert "Active polling targets: 4" in disabled.output
    configured = runner.invoke(
        cli.app,
        [
            "start-scheduler",
            "--sources-file",
            str(registry(tmp_path)),
            "--dry-run",
            "--database-url",
            "sqlite+aiosqlite://",
        ],
    )
    assert configured.exit_code == 0
    assert "greenhouse/acme" in configured.output
    assert "Active polling targets: 5" in configured.output


def test_scheduler_run_receives_validated_boards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def serve(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "_serve_scheduler", serve)
    result = CliRunner().invoke(
        cli.app,
        [
            "start-scheduler",
            "--sources-file",
            str(registry(tmp_path)),
            "--database-url",
            "sqlite+aiosqlite://",
        ],
    )
    assert result.exit_code == 0
    assert captured["ats_sources"][0].token == "acme"


@pytest.mark.parametrize("job_type", [None, "full_time", "Intern", "new-grad"])
def test_mixed_board_normalization_requires_canonical_raw_category(job_type: str | None) -> None:
    pipeline = ATSPipeline([], None, None, season=2027, job_type=None)
    with pytest.raises(ValueError):
        pipeline._normalize(
            RawJobPayload(
                source="greenhouse",
                company="Acme",
                title="Engineer Intern 2027",
                apply_url="https://example.com/jobs/1",
                job_type=job_type,
            )
        )
