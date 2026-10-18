"""One requirement specification compiled to Python and SQL predicates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Numeric, and_, case, cast, func, literal, or_, select, type_coerce
from sqlalchemy.sql.elements import ColumnElement

from app.api.v1.jobs import _array_rows
from app.db.models import Company, EvidenceCheck, JobPosting
from app.evidence.schemas import VERSION, Requirements
from app.services.job_details import us_location

MIN_ACTIVE_H1B_YEAR = 2023


def recent_h1b_summary(
    records: list[dict] | None, *, min_year: int = MIN_ACTIVE_H1B_YEAR
) -> tuple[int, int] | None:
    """Return (total_filings, latest_year) if employer has certified H-1B filings in min_year+."""
    if not records:
        return None
    valid = [
        r
        for r in records
        if isinstance(r, dict)
        and r.get("kind") in ("h1b_filings", "h1b_approvals")
        and int(r.get("count") or 0) >= 1
    ]
    if not valid or not any(int(r.get("year") or 0) >= min_year for r in valid):
        return None
    total = sum(int(r["count"]) for r in valid)
    latest = max(int(r["year"]) for r in valid)
    return total, latest


def location_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def projection(facts: dict, location: str) -> dict:
    return {
        **facts,
        "extraction_version": VERSION,
        "us_role": us_location(location),
        "location_key": location_key(location),
        "compensation": [
            {
                **pay,
                "us": us_location(pay.get("location")),
                "location_key": location_key(pay.get("location")),
            }
            for pay in facts.get("compensation", [])
        ],
    }


def floor_for(profile: Requirements, kind: str) -> Decimal | None:
    return profile.internship_hourly if kind == "internship" else profile.new_grad_annual


def evaluate(
    profile: Requirements | None,
    data: dict | None,
    *,
    kind: str,
    status: str | None,
    verified_at: datetime | None,
    now: datetime,
    closed: bool = False,
    target_current: bool = True,
    immigration_records: list[dict] | None = None,
) -> dict:
    data = data or {}
    if closed:
        return {"bucket": "excluded", "reasons": ["Applications closed"]}
    if profile is None or not profile.configured:
        return {"bucket": "review", "reasons": ["Configure your private screening requirements"]}
    if not data.get("us_role"):
        return {"bucket": "review", "reasons": ["US role/location not established"]}
    if (
        status != "complete"
        or data.get("extraction_version") != VERSION
        or not verified_at
        or not target_current
        or utc(verified_at) <= utc(now) - timedelta(hours=24)
    ):
        reason = "Not checked yet" if status is None else "Couldn’t verify current pages"
        if verified_at and utc(verified_at) <= utc(now) - timedelta(hours=24):
            reason = "Evidence is older than 24 hours"
        return {"bucket": "review", "reasons": [reason]}
    fails: list[str] = []
    reviews: list[str] = []
    passes: list[str] = []
    policies = data.get("policies", {})
    restrictions = data.get("restrictions", {})
    needs_work_auth = bool(
        (profile.authorization and profile.authorization != "unrestricted")
        or profile.future_sponsorship
    )
    if needs_work_auth:
        if any(value.get("value") == "required" for value in restrictions.values()):
            fails.append("Employer requires citizenship or permanent authorization")
        if any(value.get("value") != "required" for value in restrictions.values()):
            reviews.append("Citizenship or permanent-authorization restrictions need review")
    if profile.authorization and profile.authorization != "unrestricted":
        value = policies.get(profile.authorization, {}).get("value", "unknown")
        (passes if value == "allowed" else fails if value == "denied" else reviews).append(
            f"{profile.authorization.upper()}: "
            f"{value if value != 'unknown' else 'not stated on checked pages'}"
        )
    if profile.future_sponsorship:
        future_val = policies.get("future_sponsorship", {}).get("value", "unknown")
        spons_val = policies.get("sponsorship", {}).get("value", "unknown")
        has_restriction = bool(restrictions)
        if future_val == "allowed" or (
            future_val == "unknown" and spons_val == "allowed" and not has_restriction
        ):
            passes.append("Future sponsorship: allowed")
        elif future_val == "denied" or (future_val == "unknown" and spons_val == "denied"):
            fails.append("Future sponsorship: denied")
        elif (
            future_val == "unknown"
            and spons_val == "unknown"
            and not has_restriction
            and (h1b := recent_h1b_summary(immigration_records)) is not None
        ):
            total_filings, latest_year = h1b
            noun = "petition" if total_filings == 1 else "petitions"
            passes.append(
                f"Future sponsorship: supported by official DOL H-1B filings "
                f"(Filed {total_filings:,} H-1B {noun}, last filed {latest_year})"
            )
        else:
            label = future_val if future_val != "unknown" else spons_val
            text = label if label != "unknown" else "not stated on checked pages"
            reviews.append(f"Future sponsorship: {text}")
    accepted = {location_key(value) for value in profile.locations}
    if accepted and data.get("location_key") not in accepted:
        reviews.append("Confirm that a listed work location is acceptable")
    floor = floor_for(profile, kind)
    if floor is not None:
        period = "hour" if kind == "internship" else "year"
        pay = [
            item
            for item in data.get("compensation", [])
            if item.get("component") == "base"
            and item.get("interval") == period
            and item.get("currency") == profile.currency
            and item.get("us")
            and (not accepted or item.get("location_key") in accepted)
        ]
        amounts = [
            Decimal(str(item["maximum"] if item.get("maximum") is not None else item["minimum"]))
            for item in pay
            if item.get("maximum") is not None or item.get("minimum") is not None
        ]
        if data.get("pay_conflicting") or not amounts:
            reviews.append("Applicable base pay is conflicting or not stated on checked pages")
        elif max(amounts) >= floor:
            passes.append("Advertised range reaches your target; an offer may be below it")
        elif any(item.get("maximum") is None for item in pay):
            reviews.append("Only a below-target starting amount is stated")
        else:
            fails.append("Advertised base-pay maximum is below your target")
    return {
        "bucket": "excluded" if fails else "review" if reviews else "matches",
        "reasons": fails + reviews + passes,
    }


def _company_has_recent_h1b(
    dialect: str, min_year: int = MIN_ACTIVE_H1B_YEAR
) -> ColumnElement[bool]:
    rows = _array_rows(Company.immigration_records, dialect, "screen_h1b_rows")
    rec = type_coerce(cast(rows.c.value, JSON) if dialect == "postgresql" else rows.c.value, JSON)
    return (
        select(1)
        .select_from(Company)
        .where(
            Company.id == JobPosting.company_id,
            select(1)
            .select_from(rows)
            .where(
                rec["kind"].as_string().in_(["h1b_filings", "h1b_approvals"]),
                cast(rec["year"].as_string(), Numeric) >= min_year,
                cast(rec["count"].as_string(), Numeric) >= 1,
            )
            .exists(),
        )
        .correlate(JobPosting)
        .exists()
    )


def bucket_expression(
    profile: Requirements | None, *, dialect: str, now: datetime
) -> ColumnElement[str]:
    """SQL equivalent of evaluate; filters and count use this same expression."""
    if profile is None or not profile.configured:
        return case((JobPosting.is_closed, "excluded"), else_="review")
    data = EvidenceCheck.projection
    fresh = and_(
        EvidenceCheck.status == "complete",
        data["extraction_version"].as_string() == VERSION,
        EvidenceCheck.verified_at > now - timedelta(hours=24),
        EvidenceCheck.target["url"].as_string() == JobPosting.apply_url,
        EvidenceCheck.target["location"].as_string() == JobPosting.location,
        EvidenceCheck.target["title"].as_string() == JobPosting.title,
        data["us_role"].as_boolean().is_(True),
    )
    failures: list[Any] = []
    reviews: list[Any] = []
    needs_work_auth = bool(
        (profile.authorization and profile.authorization != "unrestricted")
        or profile.future_sponsorship
    )
    if needs_work_auth:
        failures.append(
            or_(
                data["restrictions"]["citizenship_required"]["value"].as_string() == "required",
                data["restrictions"]["permanent_authorization_required"]["value"].as_string()
                == "required",
            )
        )
        for key in ("citizenship_required", "permanent_authorization_required"):
            restriction = data["restrictions"][key]["value"].as_string()
            reviews.append(and_(restriction.is_not(None), restriction != "required"))
    if profile.authorization and profile.authorization != "unrestricted":
        value = func.coalesce(
            data["policies"][profile.authorization]["value"].as_string(), "unknown"
        )
        failures.append(value == "denied")
        reviews.append(value.not_in(["allowed", "denied"]))
    if profile.future_sponsorship:
        future_val = func.coalesce(
            data["policies"]["future_sponsorship"]["value"].as_string(), "unknown"
        )
        spons_val = func.coalesce(data["policies"]["sponsorship"]["value"].as_string(), "unknown")
        has_restriction = or_(
            data["restrictions"]["citizenship_required"]["value"].as_string().is_not(None),
            data["restrictions"]["permanent_authorization_required"]["value"]
            .as_string()
            .is_not(None),
        )
        future_failed = or_(
            future_val == "denied",
            and_(future_val == "unknown", spons_val == "denied"),
        )
        future_satisfied = or_(
            future_val == "allowed",
            and_(
                future_val == "unknown",
                ~has_restriction,
                or_(
                    spons_val == "allowed",
                    and_(spons_val == "unknown", _company_has_recent_h1b(dialect)),
                ),
            ),
        )
        failures.append(future_failed)
        reviews.append(and_(~future_failed, ~future_satisfied))
    accepted = [location_key(value) for value in profile.locations]
    if accepted:
        reviews.append(func.coalesce(data["location_key"].as_string(), "").not_in(accepted))
    # Each job category has its own explicit units; never annualize or convert.
    for kind, floor, period in [
        ("internship", profile.internship_hourly, "hour"),
        ("new_grad", profile.new_grad_annual, "year"),
    ]:
        if floor is None:
            continue
        rows = _array_rows(data["compensation"], dialect, "screen_pay_" + kind)
        pay = type_coerce(
            cast(rows.c.value, JSON) if dialect == "postgresql" else rows.c.value, JSON
        )
        constraints = [
            pay["component"].as_string() == "base",
            pay["interval"].as_string() == period,
            pay["currency"].as_string() == profile.currency,
            pay["us"].as_boolean().is_(True),
        ]
        if accepted:
            constraints.append(pay["location_key"].as_string().in_(accepted))
        amount = func.coalesce(
            cast(pay["maximum"].as_string(), Numeric), cast(pay["minimum"].as_string(), Numeric)
        )
        comparable = select(1).select_from(rows).where(*constraints, amount.is_not(None))
        meets = comparable.where(amount >= floor).exists()
        unbounded = comparable.where(pay["maximum"].as_string().is_(None)).exists()
        conflict = func.coalesce(data["pay_conflicting"].as_boolean(), False)
        failures.append(
            and_(JobPosting.job_type == kind, comparable.exists(), ~meets, ~unbounded, ~conflict)
        )
        reviews.append(
            and_(
                JobPosting.job_type == kind,
                or_(conflict, ~comparable.exists(), and_(~meets, unbounded)),
            )
        )
    fail = func.coalesce(or_(*failures), False) if failures else literal(False)
    review = func.coalesce(or_(*reviews), False) if reviews else literal(False)
    return case(
        (JobPosting.is_closed, "excluded"),
        (~func.coalesce(fresh, False), "review"),
        (fail, "excluded"),
        (review, "review"),
        else_="matches",
    )
