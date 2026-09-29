"""Persist provenance for the current location observation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_location_source"
down_revision: str | Sequence[str] | None = "7a7aec011831"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable provenance so legacy postings remain safely unknown."""
    op.add_column("job_postings", sa.Column("location_source", sa.String(length=100)))


def downgrade() -> None:
    """Remove location provenance."""
    op.drop_column("job_postings", "location_source")
