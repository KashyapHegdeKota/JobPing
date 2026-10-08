"""Conservative evidence, pay units, source authority and import contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from app.schemas.job import RawJobPayload
from app.schemas.job_details import EmployerRecord
from app.services.employer_history import parse_official_csv
from app.services.job_details import extract_job_details, merge_details
from pydantic import ValidationError

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def row(text: str = "", **updates: object) -> RawJobPayload:
    return RawJobPayload.model_validate(
        {
            "source": "greenhouse",
            "apply_url": "https://example.com/jobs/1",
            "location": "Austin, TX, US",
            "observed_at": NOW,
            "payload": {"content": text},
        }
        | updates
    )


@pytest.mark.parametrize(
    ("text", "key", "value"),
    [
        ("CPT candidates are welcome.", "cpt", "allowed"),
        ("We accept OPT candidates.", "opt", "allowed"),
        ("We do not accept CPT candidates.", "cpt", "denied"),
        ("Visa sponsorship is available.", "sponsorship", "allowed"),
        (
            "We cannot provide visa sponsorship now or in the future.",
            "future_sponsorship",
            "denied",
        ),
        ("We may offer visa sponsorship.", "sponsorship", "conditional"),
        ("CPT is accepted. CPT is not accepted.", "cpt", "conflicting"),
        ("We accept OPT but we cannot offer visa sponsorship.", "opt", "allowed"),
        ("We support STEM OPT.", "stem_opt", "allowed"),
        ("We can sponsor H-1B candidates.", "sponsorship", "allowed"),
        ("Candidates must not require visa sponsorship.", "sponsorship", "denied"),
    ],
)
def test_explicit_policy_evidence(text: str, key: str, value: str) -> None:
    details = extract_job_details(row(text))
    assert details is not None
    assert details.policies[key].value == value
    assert details.policies[key].evidence[0].observed_at == NOW
    assert str(details.policies[key].evidence[0].source_url) == "https://example.com/jobs/1"


@pytest.mark.parametrize(
    "text",
    [
        "Must be authorized to work in the United States.",
        "Will you require sponsorship?",
        "Equal opportunity employer.",
        "<script>OPT candidates accepted</script>",
        "E-Verify employer.",
    ],
)
def test_absence_questions_and_generic_authorization_are_unknown(text: str) -> None:
    assert extract_job_details(row(text)) is None


def test_stem_opt_does_not_establish_standard_opt_policy() -> None:
    details = extract_job_details(row("We support STEM OPT."))
    assert details is not None and "opt" not in details.policies


@pytest.mark.parametrize(
    ("text", "minimum", "interval", "currency"),
    [
        ("Base pay: USD 35–50 per hour", "35", "hour", "USD"),
        ("Salary: $100,000 - $150,000 per year", "100000", "year", "USD"),
        ("Pay: GBP 5k to 6k per month", "5000", "month", "GBP"),
        ("Hourly pay: USD 42 per hour", "42", "hour", "USD"),
    ],
)
def test_pay_retains_period_currency_and_evidence(
    text: str, minimum: str, interval: str, currency: str
) -> None:
    details = extract_job_details(row(text))
    assert details is not None
    pay = details.compensation[0]
    assert pay.minimum == Decimal(minimum)
    assert (pay.interval, pay.currency) == (interval, currency)
    assert text == pay.excerpt


@pytest.mark.parametrize(
    "text",
    [
        "Estimated market salary: 100k",
        "Salary: USD 100-10 per year",
        "Bonus: up to 10%",
        "Pay: $40-$50 per hour",
    ],
)
def test_no_estimates_invalid_bounds_or_ambiguous_currency(text: str) -> None:
    assert extract_job_details(row(text, location="London, UK")) is None


def test_greenhouse_cents_do_not_imply_yearly_pay() -> None:
    details = extract_job_details(
        row(
            payload={
                "pay_input_ranges": [
                    {"min_cents": 3500, "max_cents": 4500, "currency_type": "USD", "title": "NYC"}
                ]
            }
        )
    )
    assert details is not None
    assert details.compensation[0].minimum == Decimal("35")
    assert details.compensation[0].interval == "unknown"


def test_lever_structured_pay_and_source_authority() -> None:
    details = extract_job_details(
        row(
            source="lever",
            payload={
                "raw": {
                    "salaryRange": {"min": 40, "max": 50, "currency": "USD", "interval": "hour"},
                    "descriptionPlain": "We accept OPT.",
                }
            },
        )
    )
    assert details is not None and details.compensation[0].interval == "hour"
    initial = details.model_dump(mode="json")
    weak = extract_job_details(
        row(
            "We do not accept OPT.",
            source="applyguy_internships",
            observed_at=NOW + timedelta(days=1),
        )
    )
    assert merge_details(initial, weak)["policies"]["opt"]["value"] == "allowed"
    assert merge_details(initial, None) == initial
    other = extract_job_details(row("We do not accept OPT.", observed_at=NOW + timedelta(days=1)))
    assert merge_details(initial, other)["policies"]["opt"]["value"] == "conflicting"


def test_official_history_rejects_impostor_hosts() -> None:
    record = dict(
        kind="h1b_approvals", year=2025, count=5, employer_name="Acme LLC", observed_at=NOW
    )
    with pytest.raises(ValidationError):
        EmployerRecord(**record, source_url="https://uscis.gov.impostor.com/data")
    assert EmployerRecord(**record, source_url="https://www.uscis.gov/data").count == 5


def test_dol_import_counts_distinct_h1b_cases_and_exact_entities(tmp_path: Path) -> None:
    path = tmp_path / "dol.csv"
    path.write_text(
        "CASE_NUMBER,CASE_STATUS,VISA_CLASS,EMPLOYER_NAME\n"
        "1,Certified,H-1B,ACME LLC\n1,Certified,H-1B,ACME LLC\n"
        "2,Denied,H-1B,ACME LLC\n3,Certified,E-3,ACME LLC\n"
        "4,Certified,H-1B,ACME SUBSIDIARY\n",
        encoding="utf-8",
    )
    result = parse_official_csv(
        path,
        provider="dol",
        year=2025,
        source_url="https://www.dol.gov/data",
        aliases={"ACME LLC": "Acme"},
    )
    assert len(result.records) == 1
    assert result.records[0].record.count == 1
    assert result.records[0].record.kind == "h1b_filings"


def test_canadian_location_does_not_assign_us_dollars() -> None:
    assert extract_job_details(row("Pay $30-40 per hour", location="Toronto, CA")) is None


def test_aggregator_salary_is_not_employer_posted_evidence() -> None:
    assert extract_job_details(row("USD 100000-150000 per year", source="applyguy")) is None


def test_future_policy_is_derived_from_future_statement_only() -> None:
    details = extract_job_details(
        row("Visa sponsorship is available. Visa sponsorship is not available in the future.")
    )
    assert details.policies["sponsorship"].value == "conflicting"
    assert details.policies["future_sponsorship"].value == "denied"
