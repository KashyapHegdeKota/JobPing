"""Read-only database integrity diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Company, JobPosting, StatusLog


@dataclass(frozen=True, slots=True)
class AuditFinding:
    code: str
    count: int
    ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AuditReport:
    findings: tuple[AuditFinding, ...]

    @property
    def healthy(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {"healthy": self.healthy, "findings": [asdict(item) for item in self.findings]}


class DatabaseAuditService:
    """Run portable, non-mutating integrity checks."""

    def __init__(self, session: AsyncSession, *, stale_after: timedelta) -> None:
        self.session = session
        self.stale_after = stale_after

    async def run(self) -> AuditReport:
        findings: list[AuditFinding] = []
        orphan_ids = tuple(
            await self.session.scalars(
                select(JobPosting.id)
                .outerjoin(Company, JobPosting.company_id == Company.id)
                .where(Company.id.is_(None))
            )
        )
        self._add(findings, "orphan_job_postings", orphan_ids)
        for field, code in (
            (JobPosting.base_hash, "duplicate_base_hash"),
            (JobPosting.content_hash, "duplicate_content_hash"),
        ):
            duplicate_ids = tuple(
                await self.session.scalars(
                    select(JobPosting.id).where(
                        field.in_(
                            select(field).group_by(field).having(func.count(JobPosting.id) > 1)
                        )
                    )
                )
            )
            self._add(findings, code, duplicate_ids)
        closed_without_log = tuple(
            await self.session.scalars(
                select(JobPosting.id)
                .outerjoin(StatusLog, StatusLog.job_id == JobPosting.id)
                .where(JobPosting.is_closed.is_(True), StatusLog.id.is_(None))
            )
        )
        self._add(findings, "closed_without_status_log", closed_without_log)
        cutoff = datetime.now(UTC) - self.stale_after
        stale_ids = tuple(
            await self.session.scalars(
                select(JobPosting.id).where(
                    JobPosting.is_closed.is_(True), JobPosting.updated_at < cutoff
                )
            )
        )
        self._add(findings, "stale_closed_status", stale_ids)
        return AuditReport(tuple(findings))

    @staticmethod
    def _add(findings: list[AuditFinding], code: str, ids: tuple[int, ...]) -> None:
        if ids:
            findings.append(AuditFinding(code, len(ids), ids))


__all__ = ["AuditFinding", "AuditReport", "DatabaseAuditService"]
