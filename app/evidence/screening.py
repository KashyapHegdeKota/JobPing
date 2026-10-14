"""One requirement specification compiled to Python and SQL predicates."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Numeric, and_, case, cast, func, literal, or_, select, type_coerce
from sqlalchemy.sql.elements import ColumnElement

from app.api.v1.jobs import _array_rows
from app.db.models import EvidenceCheck, JobPosting
from app.evidence.schemas import VERSION, Requirements

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_US = re.compile(r"\b(?:USA|U\.?S\.?|United States)\b|,\s*(?:" + "|".join(US_STATES) + r")\b", re.I)
_FOREIGN = re.compile(
    r"\b(?:Canada|Canadian|UK|United Kingdom|London|Toronto|Vancouver|India|Germany|France|"
    r"Australia|Singapore|Mexico|China|Japan)\b",
    re.I,
)


def location_key(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def us_location(value: str | None) -> bool:
    return bool(_US.search(value or "")) and not bool(_FOREIGN.search(value or ""))


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
    if profile.authorization and profile.authorization != "unrestricted":
        if any(value.get("value") == "required" for value in restrictions.values()):
            fails.append("Employer requires citizenship or permanent authorization")
        if any(value.get("value") != "required" for value in restrictions.values()):
            reviews.append("Citizenship or permanent-authorization restrictions need review")
        value = policies.get(profile.authorization, {}).get("value", "unknown")
        (passes if value == "allowed" else fails if value == "denied" else reviews).append(
            f"{profile.authorization.upper()}: "
            f"{value if value != 'unknown' else 'not stated on checked pages'}"
        )
    if profile.future_sponsorship:
        value = policies.get("future_sponsorship", {}).get("value", "unknown")
        (passes if value == "allowed" else fails if value == "denied" else reviews).append(
            f"Future sponsorship: {value if value != 'unknown' else 'not stated on checked pages'}"
        )
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
    if profile.authorization and profile.authorization != "unrestricted":
        failures.append(
            or_(
                data["restrictions"]["citizenship_required"]["value"].as_string() == "required",
                data["restrictions"]["permanent_authorization_required"]["value"].as_string()
                == "required",
            )
        )
        value = func.coalesce(
            data["policies"][profile.authorization]["value"].as_string(), "unknown"
        )
        failures.append(value == "denied")
        reviews.append(value.not_in(["allowed", "denied"]))
        for key in ("citizenship_required", "permanent_authorization_required"):
            restriction = data["restrictions"][key]["value"].as_string()
            reviews.append(and_(restriction.is_not(None), restriction != "required"))
    if profile.future_sponsorship:
        value = func.coalesce(
            data["policies"]["future_sponsorship"]["value"].as_string(), "unknown"
        )
        failures.append(value == "denied")
        reviews.append(value.not_in(["allowed", "denied"]))
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
