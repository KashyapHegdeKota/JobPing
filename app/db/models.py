"""SQLAlchemy models for normalized job listings and their status history."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base shared by all JobPing ORM models."""


class JobType(StrEnum):
    """Job categories supported by the discovery engine."""

    INTERNSHIP = "internship"
    NEW_GRAD = "new_grad"


class ApplicationStatus(StrEnum):
    """Persisted lifecycle states for an application attempt."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    NEEDS_VERIFICATION = "needs_verification"
    NEEDS_REVIEW = "needs_review"
    READY_TO_SUBMIT = "ready_to_submit"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"


class VerificationType(StrEnum):
    """Human-completed verification mechanisms; no values are stored here."""

    EMAIL_OTP = "email_otp"
    EMAIL_MAGIC_LINK = "email_magic_link"
    SMS_OTP = "sms_otp"
    AUTHENTICATOR = "authenticator"
    CAPTCHA = "captcha"
    LOGIN = "login"
    UNKNOWN = "unknown"


class ApplicationFailure(StrEnum):
    """Small stable set of failure reasons exposed to the application agent."""

    UNKNOWN = "unknown"
    APPLICATION_CLOSED = "application_closed"
    ALREADY_APPLIED = "already_applied"
    UNSUPPORTED_FLOW = "unsupported_flow"
    MISSING_CANDIDATE_DATA = "missing_candidate_data"
    LOGIN_REQUIRED = "login_required"
    VERIFICATION_REQUIRED = "verification_required"
    BROWSER_ERROR = "browser_error"
    SUBMISSION_FAILED = "submission_failed"


class Company(Base):
    """An employer that owns one or more job postings."""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("name", name="uq_companies_name"),
        UniqueConstraint("domain", name="uq_companies_domain"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job_postings: Mapped[list[JobPosting]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )


class JobPosting(Base):
    """A normalized job listing and its current state."""

    __tablename__ = "job_postings"
    __table_args__ = (
        CheckConstraint("season >= 2020 AND season <= 2100", name="ck_job_postings_season"),
        UniqueConstraint("base_hash", name="uq_job_postings_base_hash"),
        Index("ix_job_postings_discovery", "season", "job_type", "is_closed"),
        Index("ix_job_postings_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    base_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    apply_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False)
    season: Mapped[int] = mapped_column(nullable=False)
    job_type: Mapped[JobType] = mapped_column(
        Enum(JobType, name="job_type", values_callable=lambda enum: [item.value for item in enum]),
        nullable=False,
    )
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    company: Mapped[Company] = relationship(back_populates="job_postings")
    status_logs: Mapped[list[StatusLog]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    application_attempt: Mapped[ApplicationAttempt | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )


class StatusLog(Base):
    """An immutable record of a job posting state transition."""

    __tablename__ = "status_logs"
    __table_args__ = (Index("ix_status_logs_job_changed", "job_id", "changed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    previous_state: Mapped[str | None] = mapped_column(String(32))
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[JobPosting] = relationship(back_populates="status_logs")


class ApplicationAttempt(Base):
    """Durable application checkpoint for one job.

    There is one attempt row per job.  This gives reset and resume operations a
    stable identity while preventing duplicate concurrent attempts.
    """

    __tablename__ = "application_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_application_attempts_job_id"),
        Index("ix_application_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ApplicationStatus] = mapped_column(
        Enum(
            ApplicationStatus,
            name="application_status",
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
        default=ApplicationStatus.QUEUED,
        server_default=ApplicationStatus.QUEUED.value,
    )
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_url: Mapped[str | None] = mapped_column(String(2048))
    confirmation_url: Mapped[str | None] = mapped_column(String(2048))
    verification_type: Mapped[VerificationType | None] = mapped_column(
        Enum(
            VerificationType,
            name="verification_type",
            values_callable=lambda enum: [item.value for item in enum],
        )
    )
    verification_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_instruction: Mapped[str | None] = mapped_column(String(2000))
    failure_code: Mapped[ApplicationFailure | None] = mapped_column(
        Enum(
            ApplicationFailure,
            name="application_failure",
            values_callable=lambda enum: [item.value for item in enum],
        )
    )
    failure_message: Mapped[str | None] = mapped_column(String(2000))

    job: Mapped[JobPosting] = relationship(back_populates="application_attempt")
    answers: Mapped[list[ApplicationAnswer]] = relationship(
        back_populates="application", cascade="all, delete-orphan", passive_deletes=True
    )


class ApplicationAnswer(Base):
    """A non-secret answer recorded for auditability and resume support."""

    __tablename__ = "application_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("application_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(String(2000), nullable=False)
    normalized_question: Mapped[str] = mapped_column(String(2000), nullable=False)
    answer: Mapped[str] = mapped_column(String(10000), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    confidence: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    application: Mapped[ApplicationAttempt] = relationship(back_populates="answers")
