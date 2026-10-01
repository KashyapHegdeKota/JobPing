"""Test-only browser driver validates fixture/MCP boundaries, not Codex perception."""

import pytest
from app.candidate.models import CandidateProfile
from app.db.repository import DatabaseRepository
from app.mcp.server import JobPingMCPServer
from app.schemas.job import NormalizedJob
from app.services.hasher import canonicalize_apply_url
from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.browser_e2e


async def fill_fixture(page: Page, profile: CandidateProfile) -> None:
    """Test harness only; no production application-filling code is introduced."""
    for name, value in (
        ("first_name", profile.personal.first_name),
        ("last_name", profile.personal.last_name),
        ("email", str(profile.personal.email)),
    ):
        await page.locator(f"#{name}").fill(value)
    await page.locator("#authorization").select_option("yes")
    await page.locator('[name="sponsorship"][value="no"]').check()
    await page.locator("#skills").select_option(["python", "sql"])
    await page.locator("#why_role").fill("A reviewed fixture answer.")
    await page.locator("#acknowledge").check()
    await page.locator("#resume").set_input_files(str(profile.resume_path))


async def test_controlled_discovery_browser_review_ready_explicit_confirmation(
    application_page: Page,
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    candidate_profile: CandidateProfile,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository, candidate_profile)
        queued = await server.jobs_get_next()
        assert queued is not None and queued["apply_url"] == canonicalize_apply_url(
            application_page.url
        )
        await server.application_start(posting.id, application_page.url)
        await fill_fixture(application_page, candidate_profile)
        assert await application_page.locator("#skills").evaluate(
            "el => [...el.selectedOptions].map(x => x.value)"
        ) == ["python", "sql"]
        assert (
            await application_page.locator("#resume").evaluate("el => el.files[0].name")
            == "resume.pdf"
        )
        assert (
            await application_page.locator("#attachment").text_content() == "Attached: resume.pdf"
        )
        assert await application_page.evaluate("document.querySelector('form').checkValidity()")
        await server.application_save_answer(
            posting.id, question="First name", answer="Jane", source="candidate_profile"
        )
        await server.application_update_checkpoint(posting.id, application_page.url, "review")
        await server.application_mark_ready(posting.id, application_page.url)
        await session.commit()
    async with application_sessions() as fresh:
        restarted = JobPingMCPServer(DatabaseRepository(fresh))
        assert (await restarted.application_get_checkpoint(posting.id))[
            "status"
        ] == "ready_to_submit"
        assert await application_page.evaluate("window.fixture.clicks") == 0
        assert await application_page.locator("#confirmation").is_hidden()
        # This test explicitly authorizes a click on the synthetic form only.
        await application_page.get_by_role("button", name="Submit application").click()
        assert await application_page.locator("#confirmation").is_visible()
        await restarted.application_mark_submitted(posting.id, application_page.url)
        assert (await restarted.application_get_checkpoint(posting.id))["status"] == "submitted"
        with pytest.raises(ValueError, match="invalid application transition"):
            await restarted.application_mark_submitted(posting.id, application_page.url)
        assert await application_page.evaluate("window.fixture.submissions") == 1


@pytest.mark.parametrize("mode", ["validation", "network", "unknown"])
async def test_unconfirmed_browser_result_stays_ready(
    application_page: Page,
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    candidate_profile: CandidateProfile,
    mode: str,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository, candidate_profile)
        await server.application_start(posting.id, application_page.url)
        await fill_fixture(application_page, candidate_profile)
        await server.application_mark_ready(posting.id, application_page.url)
        await application_page.evaluate("mode => window.fixture.mode = mode", mode)
        await application_page.get_by_role("button", name="Submit application").click()
        assert await application_page.locator("#confirmation").is_hidden()
        assert await application_page.locator("#error").text_content()
        checkpoint = await server.application_get_checkpoint(posting.id)
        assert checkpoint["status"] == "ready_to_submit" and checkpoint["submitted_at"] is None


@pytest.mark.parametrize("verification", ["email_otp", "captcha"])
async def test_challenge_pause_resume_never_supplies_a_secret(
    application_page: Page,
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
    verification: str,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository)
        await server.application_start(posting.id, application_page.url)
        section = "captcha" if verification == "captcha" else "verification"
        await application_page.locator(f"#{section}").evaluate("el => el.hidden = false")
        paused = await server.application_mark_verification_required(
            posting.id,
            verification_type=verification,
            current_url=application_page.url,
            instruction="Complete the challenge yourself in the browser.",
        )
        assert paused["status"] == "needs_verification"
        assert await application_page.evaluate("window.fixture.submissions") == 0
        # Simulate user completion in the controlled fixture, never solve a challenge.
        await application_page.locator(f"#{section}").evaluate("el => el.hidden = true")
        resumed = await server.application_mark_verification_complete(
            posting.id, application_page.url
        )
        assert resumed["status"] == "in_progress" and resumed["attempt_count"] == 1


async def test_unknown_question_pauses_with_blank_control(
    application_page: Page,
    application_sessions: async_sessionmaker[AsyncSession],
    application_job: NormalizedJob,
) -> None:
    async with application_sessions() as session:
        repository = DatabaseRepository(session)
        posting = await repository.save_job_posting(application_job)
        server = JobPingMCPServer(repository)
        await server.application_start(posting.id, application_page.url)
        resolution = await server.application_lookup_answer("Expected compensation?")
        assert not resolution["matched"] and resolution["requires_review"]
        reviewed = await server.application_mark_review_required(
            posting.id,
            review_question="Expected compensation?",
            review_reason="Ask candidate.",
        )
        assert reviewed["status"] == "needs_review"
        assert await application_page.locator("#compensation").input_value() == ""
        assert await application_page.evaluate("window.fixture.clicks") == 0


@pytest.mark.parametrize(
    "name, content, mime",
    [
        ("empty.pdf", b"", "application/pdf"),
        ("wrong.txt", b"text", "text/plain"),
        ("large.pdf", b"x" * (1048576 + 1), "application/pdf"),
    ],
    ids=["empty", "wrong-extension", "oversized"],
)
async def test_rejected_upload_is_visible_and_invalid(
    application_page: Page,
    name: str,
    content: bytes,
    mime: str,
) -> None:
    await application_page.locator("#resume").set_input_files(
        {"name": name, "mimeType": mime, "buffer": content}
    )
    assert not await application_page.evaluate("window.fixture.uploadAccepted")
    assert await application_page.locator("#attachment").text_content() == "Attachment rejected"
    assert not await application_page.locator("#resume").evaluate("el => el.checkValidity()")
