"""Existing deployments on either schema branch converge without rewriting history."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


@pytest.mark.parametrize(
    "starting_revision", ["0006_location_source", "0006_reposted_job_lifecycle"]
)
def test_existing_schema_branch_upgrades_to_combined_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, starting_revision: str
) -> None:
    url = f"sqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, starting_revision)
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert "location_source" in {
            column["name"] for column in inspector.get_columns("job_postings")
        }
        assert {"job_occurrences", "job_source_observations"} <= set(inspector.get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).all() == [
                ("0007_merge_location_repost",)
            ]
    finally:
        engine.dispose()
