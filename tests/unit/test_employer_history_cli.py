"""Offline command validation never opens a database or exposes invalid input."""

from pathlib import Path

from app.cli import app
from typer.testing import CliRunner


def test_history_import_invalid_input_is_sanitized(tmp_path: Path) -> None:
    file = tmp_path / "invalid.json"
    file.write_text('{"secret": "do-not-print-this"}', encoding="utf-8")
    result = CliRunner().invoke(app, ["import-employer-history", str(file), "--dry-run"])
    assert result.exit_code == 2
    assert "do-not-print-this" not in result.output


def test_official_csv_prepare_then_dry_run(tmp_path: Path) -> None:
    csv = tmp_path / "records.csv"
    aliases = tmp_path / "aliases.json"
    output = tmp_path / "history.json"
    csv.write_text(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME\n1,Certified,H-1B,ACME LLC\n",
        encoding="utf-8",
    )
    aliases.write_text('{"ACME LLC":"Acme"}', encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "prepare-employer-history",
            str(csv),
            str(aliases),
            str(output),
            "dol",
            "--year",
            "2025",
            "https://www.dol.gov/data",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["import-employer-history", str(output), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "no writes" in result.output
