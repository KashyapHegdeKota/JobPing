"""add evidence based job details and employer history

Revision ID: a7b83c96c247
Revises: 0008_analytics
Create Date: 2026-10-08 16:16:20.794172

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b83c96c247"
down_revision: str | Sequence[str] | None = "0008_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "companies",
        sa.Column("immigration_records", sa.JSON(), server_default="[]", nullable=False),
    )
    op.add_column("job_postings", sa.Column("details", sa.JSON(), nullable=True))
    op.add_column("job_occurrences", sa.Column("details", sa.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("job_occurrences", "details")
    op.drop_column("job_postings", "details")
    op.drop_column("companies", "immigration_records")
