"""durable job occurrences and occurrence-aware notifications

Revision ID: 0006_reposted_job_lifecycle
Revises: 7a7aec011831
Create Date: 2026-09-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_reposted_job_lifecycle"
down_revision: str | Sequence[str] | None = "7a7aec011831"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create occurrences, backfill silently, then migrate pending durable work."""
    op.create_table(
        "job_occurrences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("apply_url", sa.String(2048), nullable=False),
        sa.Column("identity_namespace", sa.String(255)),
        sa.Column("external_job_id", sa.String(255)),
        sa.Column("source", sa.String(100)),
        sa.Column("source_id", sa.String(500)),
        sa.Column("previous_occurrence_id", sa.Integer()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('discovered', 'reposted')", name="ck_job_occurrences_kind"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["previous_occurrence_id"], ["job_occurrences.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_job_occurrences_job_observed", "job_occurrences", ["job_id", "observed_at"])
    op.create_index(
        "ix_job_occurrences_identity",
        "job_occurrences",
        ["identity_namespace", "external_job_id"],
    )

    bind = op.get_bind()
    jobs = sa.table(
        "job_postings",
        sa.column("id", sa.Integer()),
        sa.column("apply_url", sa.String()),
        sa.column("posted_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("is_closed", sa.Boolean()),
    )
    occurrences = sa.table(
        "job_occurrences",
        sa.column("id", sa.Integer()),
        sa.column("job_id", sa.Integer()),
        sa.column("kind", sa.String()),
        sa.column("observed_at", sa.DateTime(timezone=True)),
        sa.column("posted_at", sa.DateTime(timezone=True)),
        sa.column("apply_url", sa.String()),
        sa.column("closed_at", sa.DateTime(timezone=True)),
    )
    occurrence_by_job: dict[int, int] = {}
    next_occurrence_id = 1
    for job in bind.execute(sa.select(jobs).order_by(jobs.c.id)).mappings():
        observed_at = job["created_at"]
        bind.execute(
            sa.insert(occurrences).values(
                id=next_occurrence_id,
                job_id=job["id"],
                kind="discovered",
                observed_at=observed_at,
                posted_at=job["posted_at"],
                apply_url=job["apply_url"],
                closed_at=job["updated_at"] if job["is_closed"] else None,
            )
        )
        occurrence_by_job[int(job["id"])] = next_occurrence_id
        next_occurrence_id += 1

    op.create_table(
        "job_source_observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_id", sa.String(500), nullable=False),
        sa.Column("identity_namespace", sa.String(255)),
        sa.Column("external_job_id", sa.String(255)),
        sa.Column("apply_url", sa.String(2048), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["occurrence_id"], ["job_occurrences.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "job_id", "occurrence_id", "source", "source_id", name="uq_job_source_observation"
        ),
    )
    op.create_index(
        "ix_job_source_observations_identity",
        "job_source_observations",
        ["identity_namespace", "external_job_id"],
    )

    # Rebuild the event and match tables with occurrence-based identities. Existing
    # rows are copied to the backfilled first occurrence; no events are made for
    # historical postings that did not already have one.
    op.drop_index("ix_notification_events_processed", table_name="notification_events")
    op.drop_index("ix_notification_matches_subscriber_id", table_name="notification_matches")
    op.rename_table("notification_events", "notification_events_before_occurrences")
    op.rename_table("notification_matches", "notification_matches_before_occurrences")
    op.create_table(
        "notification_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("occurrence_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["occurrence_id"], ["job_occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
    )
    op.create_index("ix_notification_events_job_id", "notification_events", ["job_id"])
    op.create_index("ix_notification_events_processed", "notification_events", ["processed"])
    op.create_table(
        "notification_matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscriber_id", sa.String(128), nullable=False),
        sa.Column("occurrence_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["occurrence_id"], ["job_occurrences.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.ForeignKeyConstraint(["subscriber_id"], ["notification_subscribers.id"]),
        sa.UniqueConstraint("subscriber_id", "occurrence_id"),
    )
    op.create_index(
        "ix_notification_matches_subscriber_id", "notification_matches", ["subscriber_id"]
    )

    old_events = sa.table(
        "notification_events_before_occurrences",
        sa.column("job_id", sa.Integer()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
        sa.column("processed", sa.Boolean()),
    )
    new_events = sa.table(
        "notification_events",
        sa.column("occurrence_id", sa.Integer()),
        sa.column("job_id", sa.Integer()),
        sa.column("event_type", sa.String()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
        sa.column("processed", sa.Boolean()),
    )
    for row in bind.execute(sa.select(old_events).order_by(old_events.c.job_id)).mappings():
        job_id = int(row["job_id"])
        bind.execute(
            sa.insert(new_events).values(
                occurrence_id=occurrence_by_job[job_id],
                job_id=job_id,
                event_type="discovered",
                discovered_at=row["discovered_at"],
                processed=row["processed"],
            )
        )

    old_matches = sa.table(
        "notification_matches_before_occurrences",
        sa.column("id", sa.Integer()),
        sa.column("subscriber_id", sa.String()),
        sa.column("job_id", sa.Integer()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
    )
    new_matches = sa.table(
        "notification_matches",
        sa.column("id", sa.Integer()),
        sa.column("subscriber_id", sa.String()),
        sa.column("occurrence_id", sa.Integer()),
        sa.column("job_id", sa.Integer()),
        sa.column("event_type", sa.String()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
    )
    for row in bind.execute(sa.select(old_matches).order_by(old_matches.c.id)).mappings():
        job_id = int(row["job_id"])
        bind.execute(
            sa.insert(new_matches).values(
                id=row["id"],
                subscriber_id=row["subscriber_id"],
                occurrence_id=occurrence_by_job[job_id],
                job_id=job_id,
                event_type="discovered",
                discovered_at=row["discovered_at"],
            )
        )
    op.drop_table("notification_matches_before_occurrences")
    op.drop_table("notification_events_before_occurrences")

    op.add_column("notification_deliveries", sa.Column("occurrence_ids", sa.JSON(), nullable=True))
    op.add_column(
        "notification_deliveries",
        sa.Column("total_matches", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("new_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("reposted_count", sa.Integer(), server_default="0", nullable=False),
    )
    deliveries = sa.table(
        "notification_deliveries",
        sa.column("id", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("job_ids", sa.JSON()),
        sa.column("occurrence_ids", sa.JSON()),
        sa.column("total_matches", sa.Integer()),
        sa.column("new_count", sa.Integer()),
        sa.column("reposted_count", sa.Integer()),
    )
    for row in bind.execute(
        sa.select(deliveries.c.id, deliveries.c.kind, deliveries.c.job_ids)
    ).mappings():
        ids = row["job_ids"] if isinstance(row["job_ids"], list) else []
        occurrence_ids = [
            occurrence_by_job[int(job_id)]
            for job_id in ids
            if str(job_id).isdigit() and int(job_id) in occurrence_by_job
        ]
        bind.execute(
            sa.update(deliveries)
            .where(deliveries.c.id == row["id"])
            .values(
                occurrence_ids=occurrence_ids,
                total_matches=len(occurrence_ids) if row["kind"] == "recap" else 0,
                new_count=len(occurrence_ids) if row["kind"] == "recap" else 0,
                reposted_count=0,
            )
        )
    with op.batch_alter_table("notification_deliveries") as batch:
        batch.alter_column("occurrence_ids", existing_type=sa.JSON(), nullable=False)
    _reset_identity_sequences(bind, ("job_occurrences", "notification_matches"))


def downgrade() -> None:
    """Restore job-keyed notifications, keeping the earliest occurrence per job."""
    bind = op.get_bind()
    occurrences = sa.table(
        "job_occurrences",
        sa.column("id", sa.Integer()),
        sa.column("job_id", sa.Integer()),
        sa.column("kind", sa.String()),
    )
    occurrence_jobs = {
        int(row["id"]): int(row["job_id"])
        for row in bind.execute(sa.select(occurrences).order_by(occurrences.c.id)).mappings()
    }

    op.create_table(
        "notification_events_before_occurrences",
        sa.Column("job_id", sa.Integer(), primary_key=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
    )
    op.create_index(
        "ix_notification_events_before_occurrences_processed",
        "notification_events_before_occurrences",
        ["processed"],
    )
    events = sa.table(
        "notification_events",
        sa.column("occurrence_id", sa.Integer()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
        sa.column("processed", sa.Boolean()),
    )
    legacy_events = sa.table(
        "notification_events_before_occurrences",
        sa.column("job_id", sa.Integer()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
        sa.column("processed", sa.Boolean()),
    )
    seen_jobs: set[int] = set()
    for row in bind.execute(
        sa.select(events).order_by(events.c.discovered_at, events.c.occurrence_id)
    ).mappings():
        job_id = occurrence_jobs[int(row["occurrence_id"])]
        if job_id not in seen_jobs:
            seen_jobs.add(job_id)
            bind.execute(
                sa.insert(legacy_events).values(
                    job_id=job_id,
                    discovered_at=row["discovered_at"],
                    processed=row["processed"],
                )
            )

    op.create_table(
        "notification_matches_before_occurrences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscriber_id", sa.String(128), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"]),
        sa.ForeignKeyConstraint(["subscriber_id"], ["notification_subscribers.id"]),
        sa.UniqueConstraint("subscriber_id", "job_id"),
    )
    op.create_index(
        "ix_notification_matches_before_occurrences_subscriber_id",
        "notification_matches_before_occurrences",
        ["subscriber_id"],
    )
    matches = sa.table(
        "notification_matches",
        sa.column("id", sa.Integer()),
        sa.column("subscriber_id", sa.String()),
        sa.column("occurrence_id", sa.Integer()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
    )
    legacy_matches = sa.table(
        "notification_matches_before_occurrences",
        sa.column("id", sa.Integer()),
        sa.column("subscriber_id", sa.String()),
        sa.column("job_id", sa.Integer()),
        sa.column("discovered_at", sa.DateTime(timezone=True)),
    )
    seen_pairs: set[tuple[str, int]] = set()
    for row in bind.execute(
        sa.select(matches).order_by(matches.c.discovered_at, matches.c.id)
    ).mappings():
        job_id = occurrence_jobs[int(row["occurrence_id"])]
        pair = (str(row["subscriber_id"]), job_id)
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            bind.execute(
                sa.insert(legacy_matches).values(
                    id=row["id"],
                    subscriber_id=pair[0],
                    job_id=job_id,
                    discovered_at=row["discovered_at"],
                )
            )

    op.drop_index("ix_notification_matches_subscriber_id", table_name="notification_matches")
    op.drop_table("notification_matches")
    op.drop_index("ix_notification_events_processed", table_name="notification_events")
    op.drop_index("ix_notification_events_job_id", table_name="notification_events")
    op.drop_table("notification_events")
    op.rename_table("notification_events_before_occurrences", "notification_events")
    op.rename_table("notification_matches_before_occurrences", "notification_matches")
    op.drop_index(
        "ix_notification_events_before_occurrences_processed", table_name="notification_events"
    )
    op.create_index("ix_notification_events_processed", "notification_events", ["processed"])
    op.drop_index(
        "ix_notification_matches_before_occurrences_subscriber_id",
        table_name="notification_matches",
    )
    op.create_index(
        "ix_notification_matches_subscriber_id", "notification_matches", ["subscriber_id"]
    )
    _reset_identity_sequences(bind, ("notification_matches",))
    op.drop_column("notification_deliveries", "occurrence_ids")
    op.drop_column("notification_deliveries", "total_matches")
    op.drop_column("notification_deliveries", "new_count")
    op.drop_column("notification_deliveries", "reposted_count")
    op.drop_index("ix_job_source_observations_identity", table_name="job_source_observations")
    op.drop_table("job_source_observations")
    op.drop_index("ix_job_occurrences_identity", table_name="job_occurrences")
    op.drop_index("ix_job_occurrences_job_observed", table_name="job_occurrences")
    op.drop_table("job_occurrences")


def _reset_identity_sequences(bind: sa.Connection, table_names: Sequence[str]) -> None:
    """Keep PostgreSQL autoincrement IDs beyond IDs copied explicitly in migration."""
    if bind.dialect.name != "postgresql":
        return
    for table_name in table_names:
        bind.execute(
            sa.text(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table_name}"
            )
        )
