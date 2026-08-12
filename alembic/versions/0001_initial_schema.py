"""Create the initial JobPing schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create company, job posting, and status history tables."""
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain", name="uq_companies_domain"),
        sa.UniqueConstraint("name", name="uq_companies_name"),
    )
    op.create_table(
        "job_postings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("base_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("apply_url", sa.String(length=2048), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column(
            "job_type",
            sa.Enum("internship", "new_grad", name="job_type"),
            nullable=False,
        ),
        sa.Column("is_closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("season >= 2020 AND season <= 2100", name="ck_job_postings_season"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("base_hash", name="uq_job_postings_base_hash"),
    )
    op.create_index("ix_job_postings_company_id", "job_postings", ["company_id"], unique=False)
    op.create_index("ix_job_postings_content_hash", "job_postings", ["content_hash"], unique=False)
    op.create_index(
        "ix_job_postings_discovery",
        "job_postings",
        ["season", "job_type", "is_closed"],
        unique=False,
    )
    op.create_table(
        "status_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("new_state", sa.String(length=32), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_status_logs_job_changed", "status_logs", ["job_id", "changed_at"], unique=False
    )


def downgrade() -> None:
    """Remove all tables introduced by the initial schema."""
    op.drop_index("ix_status_logs_job_changed", table_name="status_logs")
    op.drop_table("status_logs")
    op.drop_index("ix_job_postings_discovery", table_name="job_postings")
    op.drop_index("ix_job_postings_content_hash", table_name="job_postings")
    op.drop_index("ix_job_postings_company_id", table_name="job_postings")
    op.drop_table("job_postings")
    op.drop_table("companies")
