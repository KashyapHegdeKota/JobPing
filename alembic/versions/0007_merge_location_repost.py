"""merge location provenance and repost lifecycle

Revision ID: 0007_merge_location_repost
Revises: 0006_location_source, 0006_reposted_job_lifecycle
Create Date: 2026-10-01 15:50:12.242586

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0007_merge_location_repost"
down_revision: str | Sequence[str] | None = (
    "0006_location_source",
    "0006_reposted_job_lifecycle",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
