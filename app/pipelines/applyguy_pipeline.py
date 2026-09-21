"""ApplyGuy orchestration using the shared JobPing normalization pipeline."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.pipelines.ats_pipeline import ATSPipeline, ATSPipelineResult, ATSRejection
from app.scrapers.applyguy import ApplyGuyScraper
from app.services.deduplicator import JobDeduplicator


class ApplyGuyPipeline(ATSPipeline):
    """Normalize, globally deduplicate, and optionally persist one ApplyGuy feed."""

    def __init__(
        self,
        scraper: ApplyGuyScraper,
        deduplicator: JobDeduplicator,
        session: AsyncSession | None,
    ) -> None:
        super().__init__(
            [scraper],
            deduplicator,
            session,
            season=2027,
            job_type=scraper.job_type,
        )
        self._applyguy_scraper = scraper

    async def run(self) -> ATSPipelineResult:
        """Include row-level feed rejections in the pipeline summary."""
        result = await super().run()
        result.rejected.extend(
            ATSRejection(self._applyguy_scraper.source, source_id, reason)
            for source_id, reason in self._applyguy_scraper.rejected_rows
        )
        return result


__all__ = ["ApplyGuyPipeline"]
