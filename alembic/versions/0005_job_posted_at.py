"""job_posted_at

Revision ID: 7a7aec011831
Revises: 0004_notifications
Create Date: 2026-09-08 14:26:06.282720

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a7aec011831"
down_revision: str | Sequence[str] | None = "0004_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("job_postings", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("job_postings", "posted_at")
