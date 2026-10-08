"""Conservative extraction and reconciliation of advertised job facts."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from pydantic import ValidationError

from app.schemas.job import RawJobPayload
from app.schemas.job_details import CompensationRange, Evidence, JobDetails, PolicyFact


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "li", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_description(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _Text()
    parser.feed(html.unescape(value[:200_000]))
    return "".join(parser.parts)


_TERMS = {
    "cpt": r"\bCPT\b|curricular practical training",
    "opt": r"(?<!STEM )(?<!STEM-)\bOPT\b|(?<!STEM )optional practical training",
    "stem_opt": r"\bSTEM[- ]OPT\b",
    "sponsorship": r"\b(?:visa |immigration |H[- ]?1B )?sponsorship\b|\bsponsor(?:ing)?\b",
}
_CONDITIONAL = re.compile(r"\b(?:may|might|case.by.case|consider|depending|subject to)\b", re.I)
_PAY = re.compile(
    r"(?P<currency>USD|CAD|EUR|GBP|US\$|CA\$|\$|€|£)\s*"
    r"(?P<min>\d[\d,]*(?:\.\d{1,2})?k?)\s*(?:(?:[-–—]|to)\s*"
    r"(?:USD|CAD|EUR|GBP|US\$|CA\$|\$|€|£)?\s*"
    r"(?P<max>\d[\d,]*(?:\.\d{1,2})?k?)\s*)?(?:/|per\s+|an?\s+)?"
    r"(?P<period>hour(?:ly)?|hr|year|annum|annual(?:ly)?|month|week)\b",
    re.I,
)


def _policy_value(line: str, term: str) -> str:
    topic = f"(?:{term})"
    negative = (
        rf"\b(?:do not|does not|will not|cannot|can't|unable to|not able to)\s+"
        rf"(?:accept|support|hire|offer|provide|sponsor)\b[^.;]{{0,70}}{topic}"
        rf"|\b(?:must not|do not|cannot)\s+(?:require|need)\b[^.;]{{0,40}}{topic}"
        rf"|\bnot eligible for\s+{topic}"
        rf"|\bno\s+(?:visa |immigration |H[- ]?1B )?{topic}"
        rf"|{topic}[^.;]{{0,30}}\b(?:not accepted|not eligible|not available|"
        rf"not supported|ineligible|unavailable|not permitted)\b"
    )
    positive = (
        rf"\b(?:accept|accepts|welcome|support|supports|offer|offers|provide|provides)"
        rf"\b[^.;]{{0,60}}{topic}|{topic}[^.;]{{0,35}}\b"
        rf"(?:welcome|eligible|accepted|available|supported|permitted)\b"
        rf"|\b(?:can|will)\s+{topic}"
    )
    if re.search(negative, line, re.I):
        return "denied"
    if re.search(positive, line, re.I):
        return "conditional" if _CONDITIONAL.search(line) else "allowed"
    return "unknown"


def extract_job_details(raw: RawJobPayload) -> JobDetails | None:
    """Extract only explicit statements; absence and screening questions mean unknown."""
    payload = raw.payload
    lever = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    pieces = [
        payload.get("content"),
        payload.get("description"),
        lever.get("descriptionPlain") or lever.get("description"),
        lever.get("additionalPlain") or lever.get("additional"),
        lever.get("salaryDescriptionPlain"),
    ]
    for item in lever.get("lists", []) if isinstance(lever.get("lists"), list) else []:
        if isinstance(item, dict):
            pieces.append(item.get("content"))
    text = "\n".join(plain_description(piece) for piece in pieces)
    evidence = {
        "source": raw.source,
        "source_url": raw.apply_url,
        "observed_at": raw.observed_at or datetime.now(UTC),
    }
    policies: dict = {}
    for key, term in _TERMS.items():
        matches: list[tuple[str, Evidence]] = []
        for line in re.split(r"[\n;]|(?<=[.!])\s+|\s+but\s+", text, flags=re.I):
            line = " ".join(line.split())[:1500]
            if not line or "?" in line or not re.search(term, line, re.I):
                continue
            # Required existing authorization does not establish OPT/CPT acceptance.
            value = _policy_value(line, term)
            if value != "unknown":
                matches.append((value, Evidence(**evidence, excerpt=line)))
        if matches:
            values = {value for value, _ in matches}
            policies[key] = PolicyFact(
                value=next(iter(values)) if len(values) == 1 else "conflicting",
                evidence=[item for _, item in matches[:10]],
            )
    # Future sponsorship is separate from acceptance of current work authorization.
    future = [
        item
        for item in policies.get("sponsorship", PolicyFact()).evidence
        if re.search(r"\bfuture\b|now or (?:in the )?future", item.excerpt, re.I)
    ]
    if future:
        future_values = {_policy_value(item.excerpt, _TERMS["sponsorship"]) for item in future}
        policies["future_sponsorship"] = PolicyFact(
            value=next(iter(future_values)) if len(future_values) == 1 else "conflicting",
            evidence=future,
        )
    # Source markers are negative evidence only, using Simplify's documented legend.
    row = str(payload.get("raw_row", ""))
    if raw.source in {"github_markdown", "simplify"} and "🛂" in row:
        policies["sponsorship"] = PolicyFact(
            value="denied",
            evidence=[
                Evidence(**evidence, excerpt="Simplify marker 🛂: does not offer sponsorship")
            ],
        )
    if raw.source in {"github_markdown", "simplify"} and "🇺🇸" in row:
        for key in ("cpt", "opt", "stem_opt"):
            policies[key] = PolicyFact(
                value="denied",
                evidence=[
                    Evidence(**evidence, excerpt="Simplify marker 🇺🇸: requires U.S. citizenship")
                ],
            )
    pay: list[CompensationRange] = []
    # Aggregator descriptions cannot establish that compensation was employer-posted.
    if raw.source not in {"greenhouse", "lever", "workday", "meta", "amazon"}:
        return JobDetails(policies=policies) if policies else None
    structured = lever.get("salaryRange")
    if isinstance(structured, dict):
        try:
            pay.append(
                CompensationRange(
                    **evidence,
                    excerpt=str(structured)[:1500],
                    minimum=structured.get("min"),
                    maximum=structured.get("max"),
                    currency=str(structured.get("currency", "")).upper(),
                    interval=_interval(structured.get("interval")),
                    location=_location(raw),
                    component="unspecified",
                )
            )
        except ValidationError:
            pass
    # Greenhouse amounts are cents, but its API does not provide the pay period.
    for item in (
        payload.get("pay_input_ranges", [])
        if isinstance(payload.get("pay_input_ranges"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        try:
            pay.append(
                CompensationRange(
                    **evidence,
                    excerpt=str(item)[:1500],
                    minimum=_cents(item.get("min_cents")),
                    maximum=_cents(item.get("max_cents")),
                    currency=item.get("currency_type"),
                    interval="unknown",
                    location=item.get("title"),
                    component="unspecified",
                )
            )
        except (ValidationError, InvalidOperation, TypeError):
            continue
    if not pay:
        for line in text.splitlines():
            if re.search(
                r"market estimate|estimated market|third.party|industry average", line, re.I
            ):
                continue
            for match in _PAY.finditer(line):
                currency = {"€": "EUR", "£": "GBP", "US$": "USD", "CA$": "CAD"}.get(
                    match["currency"].upper(), match["currency"].upper()
                )
                if currency == "$":
                    if not re.search(
                        r"\b(?:US|USA|United States)\b",
                        _location(raw) or "",
                    ):
                        continue
                    currency = "USD"
                try:
                    pay.append(
                        CompensationRange(
                            **evidence,
                            excerpt=" ".join(line.split())[:1500],
                            minimum=_amount(match["min"]),
                            maximum=_amount(match["max"] or match["min"]),
                            currency=currency,
                            interval=_interval(match["period"]),
                            location=_location(raw),
                            component=(
                                "base" if re.search(r"\bbase\b", line, re.I) else "unspecified"
                            ),
                        )
                    )
                except ValidationError:
                    continue
    return JobDetails(policies=policies, compensation=pay[:20]) if policies or pay else None


def _location(raw: RawJobPayload) -> str | None:
    return "; ".join(raw.location)[:500] if isinstance(raw.location, list) else raw.location


def _amount(value: str) -> Decimal:
    return Decimal(value.lower().removesuffix("k").replace(",", "")) * (
        1000 if value.lower().endswith("k") else 1
    )


def _cents(value: object) -> Decimal | None:
    return Decimal(str(value)) / 100 if value is not None else None


def _interval(value: object) -> str:
    word = str(value).lower()
    if word in {"hour", "hourly", "hr"}:
        return "hour"
    if word in {"year", "yearly", "annual", "annually", "annum"}:
        return "year"
    return word if word in {"month", "week"} else "unknown"


def merge_details(previous: dict | None, incoming: JobDetails | None) -> dict | None:
    """Preserve meaningful observations; direct role evidence outranks aggregators."""
    if incoming is None:
        return previous
    old = JobDetails.model_validate(previous or {})
    policies = dict(old.policies)
    ranks = {
        "greenhouse": 3,
        "lever": 3,
        "workday": 3,
        "meta": 3,
        "amazon": 3,
        "github_markdown": 2,
        "simplify": 2,
    }

    def rank(fact: PolicyFact) -> int:
        return max((ranks.get(item.source, 1) for item in fact.evidence), default=0)

    for key, fact in incoming.policies.items():
        before = policies.get(key)
        if before is None or rank(fact) > rank(before):
            policies[key] = fact
        elif rank(fact) == rank(before):
            old_sources = {item.source for item in before.evidence}
            new_sources = {item.source for item in fact.evidence}
            newest = max(item.observed_at for item in fact.evidence)
            oldest = max(item.observed_at for item in before.evidence)
            if newest < oldest:
                continue
            if old_sources == new_sources or before.value == fact.value:
                policies[key] = fact
            else:
                policies[key] = PolicyFact(
                    value="conflicting", evidence=(before.evidence + fact.evidence)[-10:]
                )
    pay = old.compensation
    if incoming.compensation:
        old_rank = max((ranks.get(item.source, 1) for item in pay), default=0)
        new_rank = max(ranks.get(item.source, 1) for item in incoming.compensation)
        if new_rank >= old_rank and (
            not pay
            or max(item.observed_at for item in incoming.compensation)
            >= max(item.observed_at for item in pay)
        ):
            pay = incoming.compensation
    return JobDetails(policies=policies, compensation=pay).model_dump(mode="json")
