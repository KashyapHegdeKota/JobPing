"""Adversarial public evidence, units, privacy and deterministic screening."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from app.evidence.collect import Collector, PublicHTML
from app.evidence.extract import extract, reconcile
from app.evidence.fetch import Fetcher, FetchError, PublicTransport
from app.evidence.schemas import Document, Requirements
from app.evidence.screening import evaluate, projection

NOW = datetime(2026, 10, 12, tzinfo=UTC)
TARGET = {
    "url": "https://jobs.lever.co/acme/id",
    "namespace": "lever:acme",
    "external_id": "id",
    "company": "Acme",
    "location": "Austin, TX, US",
}


def document(text: str, **updates: object) -> Document:
    return Document.model_validate(
        {
            "source_url": TARGET["url"],
            "final_url": TARGET["url"],
            "kind": "posting",
            "text": text,
            "observed_at": NOW,
            "role_identity": TARGET,
            "location": TARGET["location"],
            "source": "lever",
            **updates,
        }
    )


@pytest.mark.parametrize(
    "text,key,value",
    [
        ("We accept OPT candidates.", "opt", "allowed"),
        ("This position does not offer visa sponsorship.", "sponsorship", "denied"),
        ("We cannot provide sponsorship now or in the future.", "future_sponsorship", "denied"),
        ("Visa sponsorship will be available in the future.", "future_sponsorship", "allowed"),
        ("We may offer visa sponsorship.", "sponsorship", "conditional"),
        ("We offer sponsorship unless the position is temporary.", "sponsorship", "conditional"),
        ("We accept CPT. We do not accept CPT.", "cpt", "conflicting"),
    ],
)
def test_assertions_and_spans(text: str, key: str, value: str) -> None:
    item = document(text)
    facts, spans = extract(item)
    assert facts["policies"][key]["value"] == value
    assert all(item.text[span["start"] : span["end"]] == span["quote"] for span in spans)


@pytest.mark.parametrize(
    "text",
    [
        "Will you require sponsorship?",
        "Are you authorized to work?",
        "We participate in E-Verify.",
        "Must be authorized to work in the US.",
        "Equal opportunity employer.",
    ],
)
def test_questions_do_not_establish_policy(text: str) -> None:
    assert extract(document(text))[0]["policies"] == {}


def test_assertion_beside_question_and_future_scope() -> None:
    assert (
        extract(document("If you do not require sponsorship please select Not Applicable."))[0][
            "policies"
        ]
        == {}
    )
    facts, _ = extract(
        document("Will you require sponsorship? This position does not offer sponsorship.")
    )
    assert facts["policies"]["sponsorship"]["value"] == "denied"
    facts, _ = extract(document("Visa sponsorship is not available in the future."))
    assert "sponsorship" not in facts["policies"]
    assert facts["policies"]["future_sponsorship"]["value"] == "denied"
    facts, _ = extract(document("We offer sponsorship to engineers building the future."))
    assert "future_sponsorship" not in facts["policies"]


def test_citizenship_is_separate_from_training_policy() -> None:
    facts, _ = extract(document("Candidates must be U.S. citizens."))
    assert facts["restrictions"]["citizenship_required"]["value"] == "required"
    assert facts["policies"] == {}


@pytest.mark.parametrize(
    "condition", ["If required by a customer, ", "Unless exempt, ", "Depending on the project, "]
)
def test_conditional_restrictions_cannot_match_allowed_opt(condition: str) -> None:
    doc = document(condition + "candidates must be US citizens. We accept OPT.")
    facts, spans = extract(doc)
    assert facts["restrictions"]["citizenship_required"]["value"] == "conditional"
    result = evaluate(
        Requirements(authorization="opt"),
        projection(facts, TARGET["location"]),
        kind="internship",
        status="complete",
        verified_at=NOW,
        now=NOW,
    )
    assert result["bucket"] == "review"
    assert all(doc.text[item["start"] : item["end"]] == item["quote"] for item in spans)


def test_restriction_conflicts_do_not_depend_on_source_order() -> None:
    first = document("Candidates must be US citizens. We accept OPT.")
    second = document(
        "Unless exempt, candidates must be US citizens.", source_url=TARGET["url"] + "/apply"
    )
    pairs = [(first, extract(first)[0]), (second, extract(second)[0])]
    assert reconcile(pairs) == reconcile(list(reversed(pairs)))
    assert reconcile(pairs)["restrictions"]["citizenship_required"]["value"] == "conflicting"
    for text in [first.text + " " + second.text, second.text + " " + first.text]:
        combined = extract(document(text))[0]
        assert combined["restrictions"]["citizenship_required"]["value"] == "conflicting"
        assert (
            evaluate(
                Requirements(authorization="opt"),
                projection(combined, TARGET["location"]),
                kind="internship",
                status="complete",
                verified_at=NOW,
                now=NOW,
            )["bucket"]
            == "review"
        )


def test_structured_pay_requires_a_public_support_span() -> None:
    doc = document(
        "No pay is stated.",
        structured_pay=[
            {
                "minimum": 100000,
                "maximum": 120000,
                "currency": "USD",
                "interval": "year",
                "component": "base",
                "excerpt": "unsupported assertion",
            }
        ],
    )
    assert extract(doc)[0]["compensation"] == []


def test_html_does_not_collect_values_hidden_text_or_scripts() -> None:
    parser = PublicHTML(TARGET["url"])
    parser.feed(
        '<label>Will you require sponsorship?</label><input value="secret">'
        "<textarea>answer-secret</textarea><p hidden>We accept OPT.</p>"
        "<script>We sponsor candidates.</script><p>Base salary USD 80000-120000 per year</p>"
    )
    assert "secret" not in parser.text and "accept OPT" not in parser.text
    assert "sponsor candidates" not in parser.text
    assert "Will you require sponsorship?" in parser.text


def test_malformed_void_end_tag_cannot_unhide_an_ancestor() -> None:
    parser = PublicHTML(TARGET["url"])
    parser.feed("<div hidden><input></input><p>hidden-secret</p></div><p>Public role</p>")
    assert "hidden-secret" not in parser.text
    assert "Public role" in parser.text


def test_text_pay_is_parsed_even_with_structured_unknown_units() -> None:
    facts, _ = extract(
        document(
            "Base salary USD 80000-120000 per year",
            structured_pay=[
                {
                    "minimum": 80000,
                    "maximum": 120000,
                    "currency": "USD",
                    "interval": "unknown",
                    "excerpt": '{"min":80000,"max":120000}',
                }
            ],
        )
    )
    assert any(
        pay["interval"] == "year" and pay["component"] == "base" for pay in facts["compensation"]
    )


@pytest.mark.parametrize(
    "text,minimum,maximum",
    [
        ("Base salary USD 80000-120000 per year", "80000", "120000"),
        ("Base salary up to USD 120000 per year", None, "120000"),
        ("Base salary starting at USD 80000 per year", "80000", None),
        ("Hourly pay USD 35 per hour", "35", "35"),
    ],
)
def test_pay_bounds(text: str, minimum: str | None, maximum: str | None) -> None:
    pay = extract(document(text))[0]["compensation"][0]
    assert (pay["minimum"], pay["maximum"]) == (minimum, maximum)


def test_company_context_conflicts_and_removal() -> None:
    role = document("We accept OPT.")
    policy = document("We do not accept OPT.", kind="company_policy")
    assert (
        reconcile([(role, extract(role)[0]), (policy, extract(policy)[0])])["policies"]["opt"][
            "value"
        ]
        == "allowed"
    )
    opposite = document("We do not accept OPT.", source_url=TARGET["url"] + "/apply")
    assert (
        reconcile([(role, extract(role)[0]), (opposite, extract(opposite)[0])])["policies"]["opt"][
            "value"
        ]
        == "conflicting"
    )
    empty = document("No policy is stated here.")
    assert reconcile([(empty, extract(empty)[0])])["policies"] == {}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Base salary USD 80000-120000 per year", "matches"),
        ("Base salary USD 80000-90000 per year", "excluded"),
        ("Base salary starting at USD 80000 per year", "review"),
        ("Base salary starting at USD 110000 per year", "matches"),
        ("Total compensation USD 100000-150000 per year", "review"),
        ("Bonus USD 100000 per year", "review"),
        ("Salary USD 100000-150000", "review"),
    ],
)
def test_maximum_matching_without_offer_guarantee(text: str, expected: str) -> None:
    doc = document(text)
    data = projection(reconcile([(doc, extract(doc)[0])]), TARGET["location"])
    result = evaluate(
        Requirements(new_grad_annual=Decimal(100000)),
        data,
        kind="new_grad",
        status="complete",
        verified_at=NOW,
        now=NOW,
    )
    assert result["bucket"] == expected


def test_stale_failed_and_unconfigured_cannot_match() -> None:
    data = {"us_role": True, "policies": {"opt": {"value": "allowed"}}}
    profile = Requirements(authorization="opt")
    for status, verified in [
        ("failed", NOW),
        ("complete", NOW - timedelta(hours=25)),
        (None, None),
    ]:
        assert (
            evaluate(
                profile, data, kind="internship", status=status, verified_at=verified, now=NOW
            )["bucket"]
            == "review"
        )
    assert (
        evaluate(
            Requirements(), data, kind="internship", status="complete", verified_at=NOW, now=NOW
        )["bucket"]
        == "review"
    )


async def test_transport_rejects_private_dns_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    async def private_dns(*args: object, **kwargs: object) -> list:
        return [(2, 1, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", private_dns)
    async with httpx.AsyncClient(transport=PublicTransport()) as client:
        with pytest.raises(FetchError, match="non_public_destination"):
            await client.get("https://public.example/")


async def test_redirect_cookie_is_not_forwarded_and_bytes_are_bounded() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final", "set-cookie": "secret=value"})
        return httpx.Response(200, text="test", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        fetcher = Fetcher(client)
        assert (await fetcher.get("https://public.example/redirect")).body == b"test"
        assert "cookie" not in requests[-1].headers
        with pytest.raises(FetchError, match="document_too_large"):
            await fetcher.get("https://public.example/final", max_bytes=2)
        with pytest.raises(FetchError, match="unsupported_destination"):
            await fetcher.get("https://user:secret@public.example/final")


async def test_ats_identity_mismatch_and_generic_employer_registry() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "different", "hostedUrl": TARGET["url"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        collector = Collector(Fetcher(client))
        with pytest.raises(FetchError, match="identity_mismatch"):
            await collector.collect(TARGET)
        with pytest.raises(FetchError, match="employer_not_reviewed"):
            await collector.collect(
                {
                    **TARGET,
                    "namespace": None,
                    "external_id": None,
                    "url": "https://acme.example/jobs/1",
                }
            )


async def test_greenhouse_questions_and_hosted_page_are_read_only() -> None:
    target = {
        **TARGET,
        "url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "namespace": "greenhouse:acme",
        "external_id": "1",
    }
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "boards-api" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "absolute_url": target["url"],
                    "content": "Base salary USD 100000-120000 per year.",
                    "questions": [
                        {"label": "Will you require sponsorship?", "fields": [{"value": "secret"}]}
                    ],
                },
            )
        return httpx.Response(
            200,
            text='<div id="content"><p>This position does not offer '
            "visa sponsorship now or in the future.</p><p>"
            + "Public posting text. " * 10
            + "</p></div>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        docs = await Collector(Fetcher(client)).collect(target)
    assert len(docs) == 2 and all(request.method == "GET" for request in requests)
    assert all("secret" not in item.text for item in docs)
    facts = reconcile([(item, extract(item)[0]) for item in docs])
    assert facts["policies"]["future_sponsorship"]["value"] == "denied"


def test_contrastive_clauses_preserve_independent_policy_polarity_and_spans() -> None:
    text = (
        "We do not currently offer visa sponsorship, but we accept OPT candidates. "
        "We may offer future visa sponsorship, however we accept CPT."
    )
    doc = document(text, location="San Francisco, California")
    facts, spans = extract(doc)
    assert facts["policies"]["sponsorship"]["value"] == "denied"
    assert facts["policies"]["opt"]["value"] == "allowed"
    assert facts["policies"]["future_sponsorship"]["value"] == "conditional"
    assert facts["policies"]["cpt"]["value"] == "allowed"
    for span in spans:
        assert doc.text[span["start"] : span["end"]] == span["quote"]


def test_spelled_out_us_state_establishes_us_role_and_location_match() -> None:
    doc = document(
        "Please note that this is an hourly position with a pay rate of $60 per hour.",
        location="San Francisco, California",
    )
    data = projection(reconcile([(doc, extract(doc)[0])]), "San Francisco, California")
    assert data["us_role"] is True
    result = evaluate(
        Requirements(internship_hourly=Decimal(50), locations=["San Francisco, California"]),
        data,
        kind="internship",
        status="complete",
        verified_at=NOW,
        now=NOW,
    )
    assert result["bucket"] == "matches"
