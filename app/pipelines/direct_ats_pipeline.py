"""Owned-resource orchestration for configured Greenhouse/Lever ingestion."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.pipelines.ats_pipeline import ATSPipeline, ATSPipelineResult, Deduplicator
from app.scrapers.ats_sources import ATSSource, ConfiguredATSScraper
from app.services.deduplicator import JobDeduplicator


@dataclass(frozen=True, slots=True)
class DirectATSRun:
    source: ATSSource
    fetched: int
    filtered: int
    result: ATSPipelineResult


async def run_direct_ats_sources(
    sources: tuple[ATSSource, ...],
    deduplicator: Deduplicator,
    *,
    session: AsyncSession,
    client: httpx.AsyncClient,
) -> tuple[DirectATSRun, ...]:
    """Run boards in registry order; failed fetches do not block later boards."""
    runs: list[DirectATSRun] = []
    for source in sources:
        async with ConfiguredATSScraper(source, client=client) as scraper:
            result = await ATSPipeline(
                [scraper], deduplicator, session, season=source.season, job_type=None
            ).run()
            runs.append(DirectATSRun(source, scraper.fetched_count, scraper.filtered_count, result))
    return tuple(runs)


async def process_direct_ats_sync(
    *, sources: tuple[ATSSource, ...], redis_url: str, database_url: str
) -> tuple[DirectATSRun, ...]:
    """Construct clients inside the active loop and release them on every exit."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            engine = create_async_engine(database_url)
            try:
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as session:
                    return await run_direct_ats_sources(
                        sources, deduplicator, session=session, client=client
                    )
            finally:
                await engine.dispose()
