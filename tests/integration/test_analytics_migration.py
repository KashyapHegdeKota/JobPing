"""Analytics schema upgrades and downgrades only an isolated local database."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_analytics_migration_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'analytics-migration.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0007_merge_location_repost")
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        assert {index["name"] for index in inspect(engine).get_indexes("analytics_events")} == {
            "ix_analytics_user_created",
            "ix_analytics_created_kind",
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO analytics_events (id, user_id, kind, page) "
                    "VALUES ('fixture', 'alice', 'page_view', '/')"
                )
            )
        command.downgrade(config, "0007_merge_location_repost")
        assert "analytics_events" not in inspect(engine).get_table_names()
        assert "job_occurrences" in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM analytics_events")) == 0
    finally:
        engine.dispose()
