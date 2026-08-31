"""Coordinate job lookup, ATS selection, and read-only browser inspection."""

from __future__ import annotations

import logging

from app.applicants.detector import ATS, detect_ats
from app.applicants.greenhouse import GreenhouseApplicant
from app.applications.models import ApplicationCheckpoint, ApplicationJob, checkpoint_from_attempt
from app.applications.queue import ApplicationQueue
from app.db.models import (
    ApplicationAnswer,
    ApplicationAttempt,
    ApplicationFailure,
    ApplicationStatus,
    VerificationType,
)
from app.db.repository import DatabaseRepository
from app.schemas.application import ApplicationForm
from app.scrapers.browser import BrowserManager

logger = logging.getLogger(__name__)


class ApplicationService:
    def __init__(
        self, repository: DatabaseRepository, browser_manager: BrowserManager | None = None
    ) -> None:
        self._repository = repository
        self._browser_manager = browser_manager

    async def get_next_job(self) -> ApplicationJob | None:
        """Return the newest eligible open job for the Codex application agent."""
        return await ApplicationQueue(self._repository).next()

    async def start_application(
        self, job_id: int, current_url: str | None = None
    ) -> ApplicationAttempt:
        """Move a queued application into progress and save its browser URL."""
        attempt = await self._repository.transition_application(
            job_id, ApplicationStatus.IN_PROGRESS, current_url=current_url
        )
        logger.info("Application %s started", job_id)
        return attempt

    async def mark_verification_required(
        self,
        job_id: int,
        verification_type: VerificationType | str,
        current_url: str,
        instruction: str,
    ) -> ApplicationAttempt:
        """Pause for user verification without accepting or storing a code."""
        attempt = await self._repository.transition_application(
            job_id,
            ApplicationStatus.NEEDS_VERIFICATION,
            current_url=current_url,
            verification_type=VerificationType(verification_type),
            resume_instruction=instruction,
        )
        logger.info("Application %s requires %s verification", job_id, attempt.verification_type)
        return attempt

    async def mark_verification_complete(
        self, job_id: int, current_url: str | None = None
    ) -> ApplicationAttempt:
        """Resume a paused application; the verification value is never supplied."""
        attempt = await self._repository.transition_application(
            job_id, ApplicationStatus.IN_PROGRESS, current_url=current_url
        )
        logger.info("Application %s resumed", job_id)
        return attempt

    async def mark_needs_review(
        self, job_id: int, current_url: str | None = None
    ) -> ApplicationAttempt:
        attempt = await self._repository.transition_application(
            job_id, ApplicationStatus.NEEDS_REVIEW, current_url=current_url
        )
        logger.info("Application %s needs review", job_id)
        return attempt

    async def mark_review_required(
        self, job_id: int, current_url: str | None = None
    ) -> ApplicationAttempt:
        """Compatibility spelling used by the MCP tool contract."""
        return await self.mark_needs_review(job_id, current_url)

    async def mark_ready_to_submit(
        self, job_id: int, current_url: str | None = None
    ) -> ApplicationAttempt:
        attempt = await self._repository.transition_application(
            job_id, ApplicationStatus.READY_TO_SUBMIT, current_url=current_url
        )
        logger.info("Application %s ready to submit", job_id)
        return attempt

    async def mark_ready(self, job_id: int, current_url: str | None = None) -> ApplicationAttempt:
        return await self.mark_ready_to_submit(job_id, current_url)

    async def mark_submitted(
        self, job_id: int, confirmation_url: str | None = None
    ) -> ApplicationAttempt:
        attempt = await self._repository.transition_application(
            job_id,
            ApplicationStatus.SUBMITTED,
            confirmation_url=confirmation_url,
        )
        logger.info("Application %s submitted", job_id)
        return attempt

    async def mark_failed(
        self,
        job_id: int,
        failure_code: ApplicationFailure | str,
        failure_message: str | None = None,
        current_url: str | None = None,
    ) -> ApplicationAttempt:
        attempt = await self._repository.transition_application(
            job_id,
            ApplicationStatus.FAILED,
            failure_code=ApplicationFailure(failure_code),
            failure_message=failure_message,
            current_url=current_url,
        )
        logger.info("Application %s failed (%s)", job_id, attempt.failure_code)
        return attempt

    async def save_answer(
        self,
        job_id: int,
        *,
        question: str,
        normalized_question: str,
        answer: str,
        source: str,
        generated: bool = False,
        confidence: float | None = None,
    ) -> ApplicationAnswer:
        """Persist one completed answer while rejecting verification secrets."""
        return await self._repository.save_application_answer(
            job_id,
            question=question,
            normalized_question=normalized_question,
            answer=answer,
            source=source,
            generated=generated,
            confidence=confidence,
        )

    async def get_checkpoint(self, job_id: int) -> ApplicationCheckpoint:
        attempt = await self._repository.get_application_attempt(job_id)
        if attempt is None:
            attempt = await self._repository.ensure_application_attempt(job_id)
        return checkpoint_from_attempt(attempt)

    async def reset_application(self, job_id: int) -> ApplicationAttempt:
        return await self._repository.reset_application(job_id)

    async def inspect_job(self, job_id: int) -> ApplicationForm:
        job = await self._repository.get_job_by_id(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")
        if job.is_closed:
            raise ValueError(f"Job {job_id} is closed")
        if not job.apply_url or not job.apply_url.startswith(("http://", "https://")):
            raise ValueError(f"Job {job_id} has no valid application URL")
        ats = detect_ats(job.apply_url)
        logger.info("Inspecting application job_id=%s; detected ATS %s", job_id, ats.value)
        if ats is not ATS.GREENHOUSE:
            raise ValueError(f"Unsupported ATS for application inspection: {ats.value}")
        if self._browser_manager is None:
            raise RuntimeError("browser manager is required for read-only inspection")
        async with self._browser_manager as browser:
            page = await browser.new_page()
            try:
                await page.goto(job.apply_url, wait_until="domcontentloaded")
                logger.info("Navigated to %s", job.apply_url)
                form = await GreenhouseApplicant().inspect(page, job_id=job_id)
            finally:
                await page.close()
        result = form.model_copy(update={"company": job.company.name, "role": job.title})
        logger.info(
            "Application inspection complete job_id=%s fields=%d", job_id, len(result.fields)
        )
        return result
