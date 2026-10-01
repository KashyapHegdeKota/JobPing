"""Read-only DOM inspection of the controlled application page."""

import pytest
from app.applicants.greenhouse import GreenhouseApplicant
from playwright.async_api import Page

pytestmark = pytest.mark.browser_e2e


async def test_inspector_reads_widgets_and_ignores_hidden_controls(application_page: Page) -> None:
    form = await GreenhouseApplicant().inspect(application_page, job_id=123)
    fields = {field.id: field for field in form.fields}
    assert set(fields) == {
        "first_name",
        "last_name",
        "email",
        "phone",
        "resume",
        "authorization",
        "sponsorship",
        "skills",
        "why_role",
        "compensation",
        "acknowledge",
    }
    assert fields["first_name"].label == "First name" and fields["first_name"].required
    assert fields["email"].field_type.value == "email"
    assert fields["resume"].field_type.value == "file" and fields["resume"].required
    assert fields["why_role"].field_type.value == "textarea"
    assert fields["sponsorship"].required
    assert [option.value for option in fields["sponsorship"].options] == ["yes", "no"]
    assert [option.value for option in fields["skills"].options] == ["python", "sql", "rust"]
    assert not fields["compensation"].required
    assert await application_page.locator("#first_name").input_value() == ""
    assert await application_page.locator("#resume").evaluate("el => el.files.length") == 0
    assert await application_page.evaluate("window.fixture.clicks") == 0


async def test_inspection_is_repeatable_and_does_not_submit(application_page: Page) -> None:
    first = await GreenhouseApplicant().inspect(application_page, job_id=123)
    second = await GreenhouseApplicant().inspect(application_page, job_id=123)
    assert first == second
    assert await application_page.evaluate("window.fixture.submissions") == 0
    assert await application_page.locator("#confirmation").is_hidden()
