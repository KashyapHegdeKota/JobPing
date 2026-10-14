"""Branded redirects, pay labels, identity rejection and contextual page boundaries."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from app.evidence.collect import Collector
from app.evidence.extract import extract, reconcile
from app.evidence.fetch import Fetcher, FetchError
from app.evidence.schemas import Document


@pytest.mark.parametrize("wrong", [None, "company", "title", "url", "duplicate"])
async def test_generic_facts_belong_only_to_exact_structured_role(wrong: str | None) -> None:
    target = {
        "url": "https://employer.example/jobs/1",
        "namespace": None,
        "external_id": None,
        "company": "Fixture",
        "title": "Software Engineer",
        "location": "Austin, TX, US",
    }
    item = {
        "@type": "JobPosting",
        "url": target["url"],
        "title": target["title"],
        "hiringOrganization": {"name": target["company"]},
        "description": "This position does not offer sponsorship. "
        + "Public role description. " * 10,
    }
    if wrong == "company":
        item["hiringOrganization"] = {"name": "Other employer"}
    if wrong == "title":
        item["title"] = "Other role"
    if wrong == "url":
        item["url"] = "https://employer.example/jobs/2"
    items = [item, item] if wrong == "duplicate" else [item]
    page = (
        '<script type="application/ld+json">'
        + json.dumps(items)
        + "</script><main><h1>Software Engineer</h1><p>Public page text. "
        + "Additional content. " * 10
        + "</p><aside>We accept OPT. Base salary USD 100000-150000 per year.</aside></main>"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html"})
        )
    ) as client:
        collector = Collector(Fetcher(client), reviewed={"employer.example": "Fixture"})
        if wrong:
            with pytest.raises(FetchError, match="role_scope_unverified"):
                await collector.collect(target)
        else:
            docs = await collector.collect(target)
            facts = reconcile([(doc, extract(doc)[0]) for doc in docs])
            assert "opt" not in facts["policies"]
            assert facts["compensation"] == []
            assert facts["policies"]["sponsorship"]["value"] == "denied"


@pytest.mark.parametrize("scope", ['id="content"', 'class="job__description body"'])
def test_known_ats_scope_excludes_recommendations_and_page_chrome(scope: str) -> None:
    from app.evidence.collect import PublicHTML

    parser = PublicHTML("https://jobs.example/1", role_only=True)
    parser.feed(
        f"<nav>We accept OPT.</nav><div {scope}><p>This role denies sponsorship.</p>"
        '<section class="recommended-jobs"><p>We accept CPT.</p></section></div>'
        "<footer>Base salary USD 120000 per year.</footer>"
    )
    assert "denies sponsorship" in parser.text
    assert "OPT" not in parser.text and "CPT" not in parser.text and "120000" not in parser.text


@pytest.mark.parametrize(
    "label,blurb,expected",
    [
        ("Annual Base Salary Range:", "", "year"),
        ("Internship", "This role has the hourly base pay rate stated below.", "hour"),
        ("Internship", "Benefits include an annual bonus.", "unknown"),
    ],
)
async def test_greenhouse_attached_units_and_branded_redirect(
    label: str, blurb: str, expected: str
) -> None:
    target = {
        "url": "https://employer.example/jobs?gh_jid=123",
        "namespace": "greenhouse:fixture",
        "external_id": "123",
        "company": "Fixture",
        "location": "Austin, TX, US",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "absolute_url": target["url"],
                    "content": "Public role",
                    "questions": [],
                    "pay_input_ranges": [
                        {
                            "title": label,
                            "blurb": blurb,
                            "min_cents": 8000000,
                            "max_cents": 12000000,
                            "currency_type": "USD",
                        }
                    ],
                },
            )
        if request.url.path == "/jobs":
            return httpx.Response(
                302, headers={"location": "https://www.employer.example/role/123?gh_jid=123"}
            )
        return httpx.Response(
            200,
            text='<main id="content"><p>' + "Public role instructions. " * 10 + "</p></main>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        docs = await Collector(Fetcher(client)).collect(target)
    result = reconcile([(item, extract(item)[0]) for item in docs])
    assert result["compensation"][0]["interval"] == expected
    if expected != "unknown":
        assert result["compensation"][0]["component"] == "base"
        assert result["compensation"][0]["location"] == target["location"]
    assert all(doc.observed_at <= datetime.now(UTC) for doc in docs)


async def test_redirect_to_different_requisition_is_rejected() -> None:
    target = {
        "url": "https://employer.example/jobs?gh_jid=123",
        "namespace": "greenhouse:fixture",
        "external_id": "123",
        "company": "Fixture",
        "location": "Austin, TX, US",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(
                200,
                json={"id": 123, "absolute_url": target["url"], "content": "Role", "questions": []},
            )
        if request.url.path == "/jobs":
            return httpx.Response(302, headers={"location": "/another-role?gh_jid=999"})
        return httpx.Response(200, text="Another role " * 20, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(FetchError, match="identity_mismatch"):
            await Collector(Fetcher(client)).collect(target)


def test_explicit_annual_prefix_and_sales_exception() -> None:
    text = (
        "The annual US base salary range for this role is $120,000 - $180,000. "
        "For sales roles, this is the role's On Target Earnings (OTE) range."
    )
    doc = Document(
        source_url="https://employer.example/job",
        final_url="https://employer.example/job",
        kind="posting",
        text=text,
        observed_at=datetime.now(UTC),
        role_identity={"title": "Software Engineer, New Grad"},
        location="New York",
    )
    pay = extract(doc)[0]["compensation"][0]
    assert pay["minimum"] == "120000" and pay["maximum"] == "180000"
    assert pay["interval"] == "year" and pay["component"] == "base"
    doc.role_identity["title"] = "Sales Associate"
    assert extract(doc)[0]["compensation"][0]["component"] == "unspecified"
