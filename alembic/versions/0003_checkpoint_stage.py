"""Persist coarse browser workflow stages for application resume."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_checkpoint_stage"
down_revision: str | None = "0002_application_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_attempts",
        sa.Column("checkpoint_stage", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "application_attempts",
        sa.Column("review_question", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "application_attempts",
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("application_attempts", "review_reason")
    op.drop_column("application_attempts", "review_question")
    op.drop_column("application_attempts", "checkpoint_stage")
