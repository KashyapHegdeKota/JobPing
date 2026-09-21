"""Repository checks for the mandatory backlog workflow."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_backlog_workflow_files_and_startup_contract_exist() -> None:
    """Keep the durable backlog contract present for future implementation agents."""
    agents = REPOSITORY_ROOT / "AGENTS.md"
    backlog = REPOSITORY_ROOT / "BACKLOG.md"

    assert backlog.is_file()
    agents_text = agents.read_text(encoding="utf-8")
    backlog_text = backlog.read_text(encoding="utf-8")

    assert "BACKLOG.md" in agents_text
    assert "Read `BACKLOG.md` completely." in agents_text
    assert "Update `BACKLOG.md`" in agents_text
    assert "INGEST-APPLYGUY-001" in backlog_text
