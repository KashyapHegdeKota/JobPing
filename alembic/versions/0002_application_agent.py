"""Add durable application checkpoints and answer audit records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_application_agent"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPLICATION_STATUS = (
    "queued",
    "in_progress",
    "needs_verification",
    "needs_review",
    "ready_to_submit",
    "submitted",
    "failed",
    "skipped",
)
VERIFICATION_TYPE = (
    "email_otp",
    "email_magic_link",
    "sms_otp",
    "authenticator",
    "captcha",
    "login",
    "unknown",
)
APPLICATION_FAILURE = (
    "unknown",
    "application_closed",
    "already_applied",
    "unsupported_flow",
    "missing_candidate_data",
    "login_required",
    "verification_required",
    "browser_error",
    "submission_failed",
)


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=True)


def upgrade() -> None:
    op.create_table(
        "application_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            _enum("application_status", APPLICATION_STATUS),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_url", sa.String(length=2048), nullable=True),
        sa.Column("confirmation_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "verification_type", _enum("verification_type", VERIFICATION_TYPE), nullable=True
        ),
        sa.Column("verification_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resume_instruction", sa.String(length=2000), nullable=True),
        sa.Column("failure_code", _enum("application_failure", APPLICATION_FAILURE), nullable=True),
        sa.Column("failure_message", sa.String(length=2000), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_application_attempts_job_id"),
    )
    op.create_index("ix_application_attempts_status", "application_attempts", ["status"])
    op.create_table(
        "application_answers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.String(length=2000), nullable=False),
        sa.Column("normalized_question", sa.String(length=2000), nullable=False),
        sa.Column("answer", sa.String(length=10000), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("generated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["application_attempts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_application_answers_application_id", "application_answers", ["application_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_application_answers_application_id", table_name="application_answers")
    op.drop_table("application_answers")
    op.drop_index("ix_application_attempts_status", table_name="application_attempts")
    op.drop_table("application_attempts")
    # PostgreSQL enum types are left to Alembic's type cleanup only after tables
    # are gone; explicit drops keep downgrade reversible on PostgreSQL.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in ("application_failure", "verification_type", "application_status"):
            sa.Enum(name=name).drop(bind, checkfirst=True)
