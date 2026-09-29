"""Upgrade and downgrade coverage for location provenance schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_location_source_migration_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "location-migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database.as_posix()}")
    config = Config("alembic.ini")

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        assert "location_source" in {
            column["name"] for column in inspect(engine).get_columns("job_postings")
        }
    finally:
        engine.dispose()

    command.downgrade(config, "7a7aec011831")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        assert "location_source" not in {
            column["name"] for column in inspect(engine).get_columns("job_postings")
        }
    finally:
        engine.dispose()
