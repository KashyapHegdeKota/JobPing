"""add private first-party analytics events

Revision ID: 0008_analytics
Revises: 0007_merge_location_repost
Create Date: 2026-10-01 16:22:40.158196

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_analytics"
down_revision: str | Sequence[str] | None = "0007_merge_location_repost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("page", sa.String(32), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("job_postings.id", ondelete="SET NULL")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_analytics_user_created", "analytics_events", ["user_id", "created_at"])
    op.create_index("ix_analytics_created_kind", "analytics_events", ["created_at", "kind"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analytics_created_kind", table_name="analytics_events")
    op.drop_index("ix_analytics_user_created", table_name="analytics_events")
    op.drop_table("analytics_events")
