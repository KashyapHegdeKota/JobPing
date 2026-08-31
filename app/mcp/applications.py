"""Application lifecycle tools for Codex; browser interaction stays in Chrome."""

from __future__ import annotations

from app.applications.models import ApplicationCheckpoint, checkpoint_from_attempt
from app.applications.resolver import AnswerResolver, normalize_question
from app.applications.service import ApplicationService
from app.db.models import ApplicationFailure, VerificationType


async def application_lookup_answer(resolver: AnswerResolver, question: str) -> dict[str, object]:
    return resolver.resolve(question).model_dump(mode="json")


async def application_start(
    service: ApplicationService, job_id: int, current_url: str
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(await service.start_application(job_id, current_url))


async def application_save_answer(
    service: ApplicationService,
    job_id: int,
    question: str,
    answer: str,
    source: str,
    *,
    generated: bool = False,
    confidence: float | None = None,
) -> dict[str, object]:
    record = await service.save_answer(
        job_id,
        question=question,
        normalized_question=normalize_question(question),
        answer=answer,
        source=source,
        generated=generated,
        confidence=confidence,
    )
    return {"id": record.id, "job_id": job_id, "question": record.question, "source": record.source}


async def application_mark_verification_required(
    service: ApplicationService,
    job_id: int,
    verification_type: VerificationType | str,
    current_url: str,
    instruction: str,
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(
        await service.mark_verification_required(
            job_id, verification_type, current_url, instruction
        )
    )


async def application_get_checkpoint(service: ApplicationService, job_id: int) -> dict[str, object]:
    return (await service.get_checkpoint(job_id)).model_dump(mode="json")


async def application_mark_verification_complete(
    service: ApplicationService, job_id: int, current_url: str | None = None
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(await service.mark_verification_complete(job_id, current_url))


async def application_mark_review_required(
    service: ApplicationService, job_id: int, current_url: str | None = None
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(await service.mark_needs_review(job_id, current_url))


async def application_mark_ready(
    service: ApplicationService, job_id: int, current_url: str | None = None
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(await service.mark_ready_to_submit(job_id, current_url))


async def application_mark_submitted(
    service: ApplicationService, job_id: int, confirmation_url: str | None = None
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(await service.mark_submitted(job_id, confirmation_url))


async def application_mark_failed(
    service: ApplicationService,
    job_id: int,
    failure_code: ApplicationFailure | str,
    failure_message: str | None = None,
    current_url: str | None = None,
) -> ApplicationCheckpoint:
    return checkpoint_from_attempt(
        await service.mark_failed(job_id, failure_code, failure_message, current_url)
    )


__all__ = [
    "application_get_checkpoint",
    "application_lookup_answer",
    "application_mark_failed",
    "application_mark_ready",
    "application_mark_review_required",
    "application_mark_submitted",
    "application_mark_verification_complete",
    "application_mark_verification_required",
    "application_save_answer",
    "application_start",
]
