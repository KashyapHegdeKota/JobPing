"""Independent failure-isolated evidence worker; ingestion never fetches documents."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import EvidenceCheck, JobPosting
from app.db.repository import DatabaseRepository
from app.events.publisher import EventPublisher
from app.evidence.collect import CollectionError, Collector
from app.evidence.fetch import Fetcher, FetchError, PublicTransport
from app.evidence.store import backfill, claim, finish, semantic

logger = logging.getLogger(__name__)


class EvidenceWorker:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        collector: Collector,
        publisher: EventPublisher | None = None,
    ) -> None:
        self.sessions = sessions
        self.collector = collector
        self.publisher = publisher

    async def process(self, claimed: tuple[int, str, dict]) -> None:
        started = time.monotonic()
        identifier, token, target = claimed
        error = None
        documents = []
        try:
            async with asyncio.timeout(60):
                documents = await self.collector.collect(target)
        except CollectionError as exc:
            error = str(exc)
            documents = exc.documents
        except FetchError as exc:
            error = str(exc)
        except TimeoutError:
            error = "check_timeout"
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            error = "source_unavailable"
        async with self.sessions() as session:
            repo = DatabaseRepository(session, publisher=self.publisher)
            async with session.begin():
                from app.db.models import JobOccurrence

                occurrence = await session.get(JobOccurrence, identifier)
                job = await session.get(JobPosting, occurrence.job_id) if occurrence else None
                before = semantic(job.details) if job else None
                saved = await finish(
                    session, identifier, token, target, documents, datetime.now(UTC), error=error
                )
                check = await session.get(EvidenceCheck, identifier)
                status = check.status if saved and check else "stale"
                conflict = bool(
                    check and check.projection and check.projection.get("pay_conflicting")
                )
                if saved and job and semantic(job.details) != before and self.publisher:
                    from app.db.models import Company

                    company = await session.get(Company, job.company_id)
                    repo._queue_job_event(job, was_created=False, company=company)
            await repo.wait_for_pending_events()
        provider = (target.get("namespace") or "").partition(":")[0]
        provider = provider if provider in {"greenhouse", "lever"} else "employer_page"
        logger.info(
            "evidence.check provider=%s status=%s error=%s documents=%s "
            "pay_conflict=%s duration_ms=%s",
            provider,
            status,
            error or "none",
            len(documents),
            conflict,
            round((time.monotonic() - started) * 1000),
        )

    async def tick(self) -> None:
        async with self.sessions() as session, session.begin():
            await backfill(session, datetime.now(UTC))
        claims = []
        for _ in range(2):
            async with self.sessions() as session, session.begin():
                selected = await claim(session, datetime.now(UTC))
                if selected:
                    claims.append(selected)
        results = await asyncio.gather(
            *(self.process(item) for item in claims), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.error("evidence.check failed type=%s", type(result).__name__)
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(EvidenceCheck.status, func.count()).group_by(EvidenceCheck.status)
                )
            ).all()
            counts = {str(status): int(count) for status, count in rows}
            queued = counts.get("queued", 0)
            oldest = await session.scalar(
                select(func.min(EvidenceCheck.requested_at)).where(EvidenceCheck.status == "queued")
            )
            logger.info(
                "evidence.heartbeat queued=%s complete=%s partial=%s failed=%s oldest=%s",
                queued,
                counts.get("complete", 0),
                counts.get("partial", 0),
                counts.get("failed", 0),
                oldest,
            )


def reviewed_employers() -> dict[str, str]:
    path = os.environ.get("EVIDENCE_EMPLOYERS_FILE")
    if not path:
        return {}
    data = Path(path).read_bytes()
    if len(data) > 100_000:
        raise ValueError("Employer registry exceeds size limit")
    parsed = json.loads(data)
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or len(key) > 255 or len(value) > 255
        for key, value in parsed.items()
    ):
        raise ValueError("Invalid reviewed employer registry")
    return parsed


async def serve(*, once: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("EVIDENCE_WORKER_ENABLED", "false").lower() != "true":
        logger.info("evidence.worker disabled")
        return
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with httpx.AsyncClient(transport=PublicTransport(), trust_env=False) as client:
            async with httpx.AsyncClient(trust_env=False, timeout=45) as renderer_client:
                async with EventPublisher.from_url() as publisher:
                    collector = Collector(
                        Fetcher(client),
                        reviewed=reviewed_employers(),
                        renderer_url=os.environ.get("EVIDENCE_RENDERER_URL"),
                        renderer_client=renderer_client,
                    )
                    worker = EvidenceWorker(
                        async_sessionmaker(engine, expire_on_commit=False), collector, publisher
                    )
                    while True:
                        await worker.tick()
                        if once:
                            return
                        await asyncio.sleep(15)
    finally:
        await engine.dispose()
