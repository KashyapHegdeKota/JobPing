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
