from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.applicants.greenhouse import GreenhouseApplicant
from app.scrapers.browser import BrowserManager
from playwright.async_api import Route


class FakePage:
    url = "https://boards.greenhouse.io/acme/jobs/1"

    async def evaluate(self, script: str) -> list[dict[str, object]]:
        assert "querySelectorAll" in script
        return [
            {
                "id": "first",
                "label": "First Name",
                "type": "text",
                "required": True,
                "options": [],
                "group": None,
            },
            {
                "id": "email",
                "label": "Email",
                "type": "email",
                "required": True,
                "options": [],
                "group": None,
            },
            {
                "id": "phone",
                "label": "Phone",
                "type": "tel",
                "required": False,
                "options": [],
                "group": None,
            },
            {
                "id": "bio",
                "label": "About",
                "type": "textarea",
                "required": False,
                "options": [],
                "group": None,
            },
            {
                "id": "country",
                "label": "Country",
                "type": "select",
                "required": True,
                "options": [{"value": "us", "label": "United States"}],
                "group": None,
            },
            {
                "id": "q1",
                "label": "Yes",
                "group_label": "Authorized to work?",
                "type": "radio",
                "required": True,
                "value": "yes",
                "options": [],
                "group": "q1",
            },
            {
                "id": "q1",
                "label": "No",
                "group_label": "Authorized to work?",
                "type": "radio",
                "required": True,
                "value": "no",
                "options": [],
                "group": "q1",
            },
            {
                "id": "skills",
                "label": "Python",
                "group_label": "Skills",
                "type": "checkbox",
                "required": False,
                "value": "python",
                "options": [],
                "group": "skills",
            },
            {
                "id": "skills",
                "label": "SQL",
                "group_label": "Skills",
                "type": "checkbox",
                "required": False,
                "value": "sql",
                "options": [],
                "group": "skills",
            },
            {
                "id": "resume",
                "label": "Resume",
                "type": "file",
                "required": True,
                "options": [],
                "group": None,
            },
            {
                "id": "start",
                "label": "Start date",
                "type": "date",
                "required": False,
                "options": [],
                "group": None,
            },
            {
                "id": "years",
                "label": "Years",
                "type": "number",
                "required": False,
                "options": [],
                "group": None,
            },
            {
                "id": "mystery",
                "label": "Mystery",
                "type": "unknown",
                "required": False,
                "options": [],
                "group": None,
            },
        ]


@pytest.mark.asyncio
async def test_greenhouse_normalizes_types_options_groups_and_labels() -> None:
    form = await GreenhouseApplicant().inspect(FakePage(), job_id=1)
    assert Path("tests/fixtures/greenhouse/basic_form.html").exists()
    assert {field.field_type.value for field in form.fields} >= {
        "text",
        "email",
        "tel",
        "textarea",
        "select",
        "radio",
        "checkbox",
        "file",
        "date",
        "number",
        "unknown",
    }
    radio = next(field for field in form.fields if field.id == "q1")
    assert radio.label == "Authorized to work?"
    assert len(radio.options) == 2
    assert [option.value for option in radio.options] == ["yes", "no"]
    assert sum(field.id == "q1" for field in form.fields) == 1
    assert sum(field.id == "skills" for field in form.fields) == 1
    assert (
        next(field for field in form.fields if field.id == "country").options[0].label
        == "United States"
    )
    assert next(field for field in form.fields if field.id == "email").required


@pytest.mark.browser_e2e
@pytest.mark.skipif(
    os.getenv("RUN_BROWSER_E2E") != "1",
    reason="set RUN_BROWSER_E2E=1 to run local Chromium DOM inspection",
)
@pytest.mark.parametrize(
    ("fixture_name", "expected_ids"),
    [
        ("basic_form.html", {"first_name", "email", "phone", "resume"}),
        ("custom_questions.html", {"question_1", "skills", "why"}),
        (
            "complex_form.html",
            {
                "start",
                "count",
                "country",
                "portfolio",
                "referrer",
                "missing_label",
                "duplicate_one",
                "duplicate_two",
                "custom_select",
            },
        ),
    ],
)
async def test_greenhouse_inspects_local_html_fixture(
    fixture_name: str, expected_ids: set[str]
) -> None:
    html = Path("tests/fixtures/greenhouse", fixture_name).read_text(encoding="utf-8")

    async with BrowserManager(headless=True, block_resources=frozenset()) as browser:
        page = await browser.new_page()

        async def serve_fixture(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route("**/*", serve_fixture)
        await page.goto(f"https://boards.greenhouse.io/acme/{fixture_name}")
        form = await GreenhouseApplicant().inspect(page, job_id=481)

    fields = {field.id: field for field in form.fields}
    assert set(fields) == expected_ids
    if fixture_name == "basic_form.html":
        assert fields["first_name"].label == "First Name"
        assert fields["first_name"].required is True
        assert fields["resume"].field_type.value == "file"
    elif fixture_name == "custom_questions.html":
        assert fields["question_1"].label == "Are you legally authorized to work?"
        assert [option.label for option in fields["question_1"].options] == ["Yes", "No"]
        assert [option.label for option in fields["skills"].options] == ["Python", "SQL"]
    else:
        assert fields["country"].options[0].label == "United States"
        assert fields["portfolio"].label == "Portfolio"
        assert fields["referrer"].label == "How did you hear about us?"
        assert fields["missing_label"].label == "missing_label"
        assert fields["duplicate_one"].label == fields["duplicate_two"].label == "Website"
        assert fields["portfolio"].field_type.value == "unknown"
        assert fields["custom_select"].field_type.value == "select"
