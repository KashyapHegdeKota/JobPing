"""Deterministic tests for the Chrome/MCP application backend boundary."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from app.applications.resolver import AnswerResolver, normalize_question
from app.applications.service import ApplicationService
from app.candidate.models import CandidateProfile
from app.db.models import ApplicationStatus, Base, VerificationType
from app.db.repository import DatabaseRepository
from app.mcp.jobs import jobs_get_next
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from sqlalchemy import create_engine, inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class _ApplicationFixtureParser(HTMLParser):
    """Extract the browser-visible controls from the local workflow fixture."""

    def __init__(self) -> None:
        super().__init__()
        self.controls: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        name = attributes.get("name")
        if name and tag in {"input", "select", "textarea"}:
            self.controls[name] = "select" if tag == "select" else attributes.get("type", "text")


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def job(hash_value: str, *, closed: bool = False) -> NormalizedJob:
    return NormalizedJob(
        company_name="Acme",
        title=f"Engineer {hash_value[0]}",
        base_hash=hash_value * 64,
        content_hash=("b" if hash_value == "a" else "c") * 64,
        apply_url=f"https://example.com/{hash_value}",
        location="Remote",
        season=2027,
        job_type="internship",
        is_closed=closed,
    )


async def test_application_state_transitions_and_invalid_transition(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    posting = await repository.save_job_posting(job("a"))
    service = ApplicationService(repository)

    assert (
        await service.start_application(posting.id, "https://example.com/a")
    ).status is ApplicationStatus.IN_PROGRESS
    initial = await repository.get_application_attempt(posting.id)
    assert initial is not None and initial.attempt_count == 1
    with pytest.raises(ValueError, match="invalid application transition"):
        await service.mark_submitted(posting.id)
    attempt = await service.mark_verification_required(
        posting.id,
        VerificationType.EMAIL_OTP,
        "https://example.com/a",
        "Complete verification in Chrome.",
    )
    assert attempt.status is ApplicationStatus.NEEDS_VERIFICATION
    assert attempt.verification_type is VerificationType.EMAIL_OTP
    assert not hasattr(attempt, "otp")
    resumed = await service.mark_verification_complete(posting.id, "https://example.com/a")
    assert resumed.attempt_count == 1
    checkpoint = await service.get_checkpoint(posting.id)
    assert checkpoint.verification_type is None
    assert checkpoint.resume_instruction is None
    assert resumed.resume_instruction is None
    await service.mark_ready(posting.id, "https://example.com/a")
    await service.mark_submitted(posting.id, "https://example.com/confirmation")
    with pytest.raises(ValueError, match="invalid application transition"):
        await service.start_application(posting.id, "https://example.com/a")


async def test_queue_filters_closed_and_submitted_jobs(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    open_job = await repository.save_job_posting(job("a"))
    closed_job = await repository.save_job_posting(job("c", closed=True))
    await repository.save_job_posting(job("d"))
    await ApplicationService(repository).start_application(open_job.id)
    await ApplicationService(repository).mark_ready(open_job.id)
    await ApplicationService(repository).mark_submitted(open_job.id)
    result = await jobs_get_next(repository)
    assert result is not None
    assert result["job_id"] != open_job.id
    assert result["job_id"] != closed_job.id


async def test_queue_excludes_every_non_queued_application_state(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    eligible = await repository.save_job_posting(job("a"))
    closed = await repository.save_job_posting(job("b", closed=True))
    blocked_postings = {
        status: await repository.save_job_posting(job(letter))
        for letter, status in zip(
            "1234567",
            (
                ApplicationStatus.SUBMITTED,
                ApplicationStatus.SKIPPED,
                ApplicationStatus.IN_PROGRESS,
                ApplicationStatus.NEEDS_VERIFICATION,
                ApplicationStatus.NEEDS_REVIEW,
                ApplicationStatus.READY_TO_SUBMIT,
                ApplicationStatus.FAILED,
            ),
            strict=True,
        )
    }
    for status, posting in blocked_postings.items():
        attempt = await repository.ensure_application_attempt(posting.id)
        attempt.status = status
    await session.flush()
    queue = await repository.list_application_queue()
    assert [attempt.job_id for attempt in queue] == [eligible.id]
    assert closed.id not in {attempt.job_id for attempt in queue}


async def test_answer_storage_rejects_secrets_and_empty_sources(session: AsyncSession) -> None:
    repository = DatabaseRepository(session)
    posting = await repository.save_job_posting(job("a"))
    for question, answer in (
        ("Enter code", "123456"),
        ("Security question", "verification code 123456"),
        ("Authenticator", "12345678"),
    ):
        with pytest.raises(ValueError, match="must not be stored"):
            await repository.save_application_answer(
                posting.id,
                question=question,
                normalized_question=normalize_question(question),
                answer=answer,
                source="human",
            )
    with pytest.raises(ValueError, match="source must not be empty"):
        await repository.save_application_answer(
            posting.id,
            question="Why?",
            normalized_question="why",
            answer="A reviewed answer.",
            source="  ",
        )
    await repository.transition_application(posting.id, ApplicationStatus.IN_PROGRESS)
    with pytest.raises(ValueError, match="must not be stored"):
        await repository.transition_application(
            posting.id,
            ApplicationStatus.NEEDS_VERIFICATION,
            verification_type=VerificationType.EMAIL_OTP,
            current_url="https://example.com/a",
            resume_instruction="The code is 123456.",
        )
    with pytest.raises(ValueError, match="must not be stored"):
        await repository.transition_application(
            posting.id,
            ApplicationStatus.NEEDS_VERIFICATION,
            verification_type=VerificationType.EMAIL_OTP,
            current_url="https://example.com/a",
            resume_instruction="123456",
        )

    with pytest.raises(ValueError, match="must not be stored") as review_error:
        await ApplicationService(repository).mark_review_required(
            posting.id,
            current_url="https://example.com/a",
            review_question="Enter the verification code",
            review_reason="The code is 123456.",
        )
    assert "123456" not in str(review_error.value)


async def test_mcp_server_handlers_return_safe_business_data(
    session: AsyncSession, tmp_path: Path
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"sanitized")
    profile = CandidateProfile.model_validate(
        {
            "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
            "education": {"school": "Example U", "degree": "BS", "major": "CS"},
            "links": {},
            "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
            "resume_path": str(resume),
        }
    )
    repository = DatabaseRepository(session)
    posting = await repository.save_job_posting(job("a"))
    server = JobPingMCPServer(repository, profile)

    assert (await server.jobs_get_next())["job_id"] == posting.id
    assert (await server.jobs_get(posting.id))["apply_url"] == posting.apply_url
    assert server.candidate_get_profile()["personal"]["email"] == "jane@example.com"
    assert server.candidate_get_resume_path() == str(resume)
    lookup = await server.application_lookup_answer("What is your first name?")
    assert lookup["matched"] is True and lookup["source"] == "candidate_profile"

    started = await server.application_start(posting.id, posting.apply_url)
    assert started["status"] == "in_progress"
    saved = await server.application_save_answer(
        posting.id, question="Why?", answer="A reviewed answer.", source="human"
    )
    assert saved["source"] == "human"
    paused = await server.application_mark_verification_required(
        posting.id,
        verification_type="email_otp",
        current_url=posting.apply_url,
        instruction="Enter the code in Chrome.",
    )
    assert paused["status"] == "needs_verification"
    checkpoint = await server.application_get_checkpoint(posting.id)
    assert checkpoint["verification_type"] == "email_otp"
    resumed = await server.application_mark_verification_complete(posting.id, posting.apply_url)
    assert resumed["status"] == "in_progress"
    ready = await server.application_mark_ready(posting.id, posting.apply_url)
    assert ready["status"] == "ready_to_submit"
    submitted = await server.application_mark_submitted(posting.id, "https://example.com/thanks")
    assert submitted["status"] == "submitted"

    review_posting = await repository.save_job_posting(job("d"))
    await server.application_start(review_posting.id, review_posting.apply_url)
    reviewed = await server.application_mark_review_required(review_posting.id)
    assert reviewed["status"] == "needs_review"

    failed_posting = await repository.save_job_posting(job("e"))
    await server.application_start(failed_posting.id, failed_posting.apply_url)
    failed = await server.application_mark_failed(
        failed_posting.id, failure_code="browser_error", failure_message="Browser unavailable."
    )
    assert failed["status"] == "failed"


async def test_phase2_checkpoint_subsets_stories_and_closed_failure(
    session: AsyncSession, tmp_path: Path
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "application_agent" / "application_flow.html"
    fixture_parser = _ApplicationFixtureParser()
    fixture_parser.feed(fixture.read_text(encoding="utf-8"))
    assert {
        "first_name",
        "last_name",
        "email",
        "phone",
        "resume",
        "work_authorization",
        "sponsorship",
        "why_role",
        "degree",
        "acknowledge",
    }.issubset(fixture_parser.controls)
    assert fixture_parser.controls["resume"] == "file"
    assert "Verify your email" in fixture.read_text(encoding="utf-8")
    assert "Thank you for applying" in fixture.read_text(encoding="utf-8")

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"sanitized")
    profile = CandidateProfile.model_validate(
        {
            "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
            "education": {"school": "Example U", "degree": "BS", "major": "CS"},
            "links": {},
            "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
            "resume_path": str(resume),
        }
    )
    resolver = AnswerResolver(
        profile, stories={"stories": {"project": {"summary": "Reviewed project."}}}
    )
    repository = DatabaseRepository(session)
    posting = await repository.save_job_posting(job("a"))
    server = JobPingMCPServer(repository, profile, resolver=resolver)

    started = await server.application_start(posting.id, posting.apply_url)
    checkpoint = await server.application_update_checkpoint(
        posting.id, "https://example.com/form", "application_form"
    )
    assert started["status"] == "in_progress"
    assert checkpoint["stage"] == "application_form"
    assert checkpoint["current_url"] == "https://example.com/form"
    assert server.candidate_get_contact()["email"] == "jane@example.com"
    assert server.candidate_get_education()["school"] == "Example U"
    assert server.candidate_get_work_authorization()["authorized_us"] is True
    assert server.stories_get(story_ids=["project"])["story_ids"] == ["project"]

    closed = await repository.save_job_posting(job("c", closed=True))
    failed = await server.application_mark_failed(
        closed.id, failure_code="application_closed", failure_message="Position removed."
    )
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "application_closed"


async def test_review_metadata_checkpoint_safety_and_already_applied(
    session: AsyncSession,
) -> None:
    repository = DatabaseRepository(session)
    posting = await repository.save_job_posting(job("a"))
    server = JobPingMCPServer(repository)

    with pytest.raises(ValueError, match="must be started"):
        await server.application_update_checkpoint(
            posting.id, "https://example.com/form", "application_form"
        )
    assert await repository.get_application_attempt(posting.id) is None

    await server.application_start(posting.id, posting.apply_url)
    with pytest.raises(ValueError, match="inconsistent"):
        await server.application_update_checkpoint(
            posting.id, "https://example.com/form", "submitted"
        )
    updated = await server.application_update_checkpoint(
        posting.id, "https://example.com/form", "application_form"
    )
    assert updated["status"] == "in_progress"

    reviewed = await server.application_mark_review_required(
        posting.id,
        current_url="https://example.com/form",
        review_question="What is your SAT score?",
        review_reason="Candidate profile does not contain this factual value.",
    )
    assert reviewed["status"] == "needs_review"
    assert reviewed["review_question"] == "What is your SAT score?"
    assert reviewed["review_reason"].startswith("Candidate profile")
    await server.service.reset_application(posting.id)
    reset = await server.application_get_checkpoint(posting.id)
    assert reset["review_question"] is None and reset["review_reason"] is None

    await server.application_start(posting.id, posting.apply_url)
    failed = await server.application_mark_failed(
        posting.id,
        failure_code="already_applied",
        failure_message="Employer reports an existing application.",
    )
    assert failed["failure_code"] == "already_applied"


def test_question_normalization_and_protected_resolution() -> None:
    assert normalize_question("  Need sponsorship? ") == "need sponsorship"
    resolver = AnswerResolver()
    result = resolver.resolve("What is your citizenship status?")
    assert result.matched is False
    assert result.generation_allowed is False
    assert result.requires_review is True


def test_answer_bank_and_generation_resolution() -> None:
    resolver = AnswerResolver(
        answers={
            "application_answers": [
                {"patterns": ["why this company"], "answer": "A reviewed answer."}
            ]
        },
        stories={"stories": {"project": {"summary": "source"}}},
    )
    matched = resolver.resolve("Why are you interested in this company?")
    assert matched.answer == "A reviewed answer."
    generated = resolver.resolve("Describe a technical challenge you solved")
    assert generated.generation_allowed is True
    assert generated.relevant_stories == ["project"]


def test_structured_answer_bank_resolves_known_repeated_answers() -> None:
    resolver = AnswerResolver(
        answers={
            "work_authorization": {
                "authorized_us": "Yes",
                "requires_sponsorship": "No",
            },
            "availability": {"relocate": "Yes"},
        }
    )
    authorized = resolver.resolve("Are you legally authorized to work?")
    sponsorship = resolver.resolve("Will you require visa sponsorship?")
    relocation = resolver.resolve("Are you willing to relocate?")
    assert (authorized.answer, authorized.source) == ("Yes", "answer_bank")
    assert (sponsorship.answer, sponsorship.source) == ("No", "answer_bank")
    assert (relocation.answer, relocation.source) == ("Yes", "answer_bank")


def test_custom_never_invent_category_is_protected() -> None:
    resolver = AnswerResolver(rules={"never_invent": ["disability_status"]})
    result = resolver.resolve("Please provide your disability status")
    assert result.requires_review is True
    assert result.generation_allowed is False


def test_application_migration_upgrade_and_downgrade_on_temporary_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "migration.sqlite3"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"application_attempts", "application_answers"}.issubset(tables)
        columns = {
            item["name"]: item for item in inspect(engine).get_columns("application_attempts")
        }
        assert "queued" in str(columns["status"]["default"] or "")
        assert {"checkpoint_stage", "review_question", "review_reason"}.issubset(columns)
    finally:
        engine.dispose()
    command.downgrade(config, "base")
    engine = create_engine(f"sqlite:///{database}")
    try:
        assert "application_attempts" not in inspect(engine).get_table_names()
        assert "application_answers" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_official_mcp_stdio_protocol_lists_exact_tools_and_calls_profile(
    tmp_path: Path,
) -> None:
    from mcp.client.stdio import stdio_client

    from mcp import ClientSession, StdioServerParameters

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"sanitized")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps(
            {
                "personal": {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com"},
                "education": {"school": "Example U", "degree": "BS", "major": "CS"},
                "links": {},
                "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
                "resume_path": str(resume),
            }
        ),
        encoding="utf-8",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp.server"],
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'mcp.sqlite3'}",
            "JOBPING_CANDIDATE_PATH": str(candidate),
        },
    )
    expected = {
        "jobs_get_next",
        "jobs_get",
        "candidate_get_profile",
        "candidate_get_resume_path",
        "candidate_get_contact",
        "candidate_get_education",
        "candidate_get_links",
        "candidate_get_work_authorization",
        "application_lookup_answer",
        "stories_get",
        "application_start",
        "application_update_checkpoint",
        "application_save_answer",
        "application_mark_verification_required",
        "application_get_checkpoint",
        "application_mark_verification_complete",
        "application_mark_review_required",
        "application_mark_ready",
        "application_mark_submitted",
        "application_mark_failed",
    }
    async with stdio_client(parameters) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == expected
            result = await client.call_tool("candidate_get_profile", {})
            assert result.is_error is False
            assert result.content[0].text is not None
            payload = json.loads(result.content[0].text)
            assert payload["personal"]["first_name"] == "Jane"
