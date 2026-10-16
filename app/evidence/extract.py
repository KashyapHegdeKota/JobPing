"""Versioned, source-span-backed rules. No model inference or company-history joins."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError

from app.evidence.schemas import Document
from app.schemas.job import RawJobPayload
from app.schemas.job_details import CompensationRange, Evidence, JobDetails, PolicyFact
from app.services.job_details import (
    _TERMS,
    _amount,
    _policy_value,
    extract_job_details,
    us_location,
)

_PREFIX_PAY = re.compile(
    r"(?P<period>annual|yearly|hourly|monthly|weekly)\s+(?P<us>US\s+)?"
    r"base\s+(?:salary|pay|wage|rate)(?:\s+range)?[^\n.;$€£]{0,70}"
    r"(?P<currency>USD|CAD|EUR|GBP|US\$|CA\$|\$|€|£)\s*"
    r"(?P<min>\d[\d,]*(?:\.\d{1,2})?k?)\s*"
    r"(?:(?:[-–—]|to)\s*(?:USD|CAD|EUR|GBP|US\$|CA\$|\$|€|£)?\s*"
    r"(?P<max>\d[\d,]*(?:\.\d{1,2})?k?))?",
    re.I,
)
_CLAUSE = re.compile(
    r"(?:(?!,?\s+\b(?:but|however|although|though|while|whereas)\b\s+).)+",
    re.I | re.S,
)


def _merge_restriction(previous: dict | None, current: dict) -> dict:
    if previous is None:
        return current
    return {
        "value": previous["value"] if previous["value"] == current["value"] else "conflicting",
        "evidence": (previous["evidence"] + current["evidence"])[:10],
    }


def extract(document: Document) -> tuple[dict, list[dict]]:
    """Facts contain only quoted assertions applicable to a verified role document."""
    if document.kind == "company_policy":
        # A linked company policy is useful context, never an automatic role assertion.
        return {"policies": {}, "compensation": [], "restrictions": {}}, []
    facts: dict = {"policies": {}, "compensation": [], "restrictions": {}}
    spans: list[dict] = []
    matches: dict[str, list[tuple[str, dict]]] = {}
    scan = re.sub(r"U\.S\.", "US  ", document.text)
    for sentence in re.finditer(r"[^\n;.!?]+(?:[.!?]|$)", scan):
        line_start = scan.rfind("\n", 0, sentence.start()) + 1
        line_end = scan.find("\n", sentence.start())
        context = scan[line_start : line_end if line_end >= 0 else len(scan)]
        if re.search(
            r"please select|choose an option|if yes|not applicable", context, re.I
        ) and not re.search(
            r"\bthis (?:role|position)|\bwe (?:do not|cannot|will not|offer|provide|accept)",
            context,
            re.I,
        ):
            continue
        if context.lstrip().startswith("Question:") and not re.search(
            r"\bthis (?:role|position)|\bwe (?:do not|cannot|will not|offer|provide|accept)",
            context,
            re.I,
        ):
            continue
        whole = sentence.group().strip()
        if not whole or whole.endswith("?") or len(whole) > 1500:
            continue
        for clause in _CLAUSE.finditer(sentence.group()):
            start = sentence.start() + clause.start()
            end = sentence.start() + clause.end()
            original = document.text[start:end]
            line = clause.group().strip()
            if not line or line.endswith("?"):
                continue
            evidence = Evidence(
                source=document.source,
                source_url=document.final_url,
                excerpt=original.strip(),
                observed_at=document.observed_at,
            ).model_dump(mode="json")
            for key, term in _TERMS.items():
                if not re.search(term, line, re.I):
                    continue
                value = _policy_value(line, term)
                # Statements about referrals, events, or sponsorship questions are not policy.
                if re.search(
                    r"\b(?:if|unless|except|depending|subject to|may|might|case.by.case)\b",
                    line,
                    re.I,
                ):
                    if value != "unknown":
                        value = "conditional"
                if value == "unknown":
                    continue
                is_future = bool(
                    re.search(
                        r"\bfuture\s+(?:visa\s+)?sponsorship\b|"
                        r"\bsponsorship\s+(?:(?:is|will|be|not|available|provided|offered|"
                        r"supported|now|or|and|currently|either|both)\s+){0,14}"
                        r"in\s+(?:the\s+)?future\b",
                        line,
                        re.I,
                    )
                )
                if key == "sponsorship" and is_future:
                    matches.setdefault("future_sponsorship", []).append((value, evidence))
                    spans.append(
                        {
                            "start": start,
                            "end": end,
                            "field": "future_sponsorship",
                            "quote": original,
                        }
                    )
                    # A future-only restriction does not deny current support.
                    if not re.search(r"\bnow\b|\bcurrently\b|\bat any time\b", line, re.I):
                        continue
                matches.setdefault(key, []).append((value, evidence))
                spans.append({"start": start, "end": end, "field": key, "quote": original})
            restrictions = {
                "citizenship_required": (
                    r"\b(?:must be|requires?|only|restricted to)\s+(?:a\s+)?"
                    r"(?:U\.?S\.?|United States)\s+citizens?\b"
                ),
                "permanent_authorization_required": (
                    r"\b(?:must have|requires?|only)\s+"
                    r"(?:unrestricted|permanent)\s+"
                    r"(?:U\.?S\.?\s+)?work authorization\b"
                ),
            }
            for key, pattern in restrictions.items():
                if re.search(pattern, line, re.I):
                    restriction = {
                        "value": (
                            "conditional"
                            if re.search(
                                r"\b(?:if|unless|except|depending|subject to|may|might|"
                                r"case.by.case)\b",
                                line,
                                re.I,
                            )
                            else "required"
                        ),
                        "evidence": [evidence],
                    }
                    facts["restrictions"][key] = _merge_restriction(
                        facts["restrictions"].get(key), restriction
                    )
                    spans.append(
                        {
                            "start": start,
                            "end": end,
                            "field": key,
                            "quote": original,
                        }
                    )
    for key, values in matches.items():
        states = {value for value, _ in values}
        facts["policies"][key] = {
            "value": sorted(states)[0] if len(states) == 1 else "conflicting",
            "evidence": [item for _, item in values[:10]],
        }
    # Parse description independently even when structured pay exists.
    raw = RawJobPayload(
        source="greenhouse",
        apply_url=document.final_url,
        location=document.location,
        observed_at=document.observed_at,
        payload={"content": document.text},
    )
    parsed = extract_job_details(raw)
    text_pay = parsed.compensation if parsed else []
    ambiguous_prefixes: set[int] = set()
    for match in _PREFIX_PAY.finditer(document.text):
        currency = {"$": "USD", "US$": "USD", "CA$": "CAD", "€": "EUR", "£": "GBP"}.get(
            match["currency"].upper(), match["currency"].upper()
        )
        if match["currency"] == "$" and not (match["us"] or us_location(document.location)):
            continue
        paragraph_start = document.text.rfind("\n", 0, match.start()) + 1
        paragraph_end = document.text.find("\n", match.end())
        paragraph = document.text[
            paragraph_start : paragraph_end if paragraph_end >= 0 else len(document.text)
        ]
        if re.search(r"estimate|third.party|industry average", paragraph, re.I):
            continue
        component = "base"
        if "OTE" in paragraph or re.search(r"on.target earnings", paragraph, re.I):
            title = document.role_identity.get("title") or ""
            if not title or re.search(r"sales|account executive|business development", title, re.I):
                component = "unspecified"
        try:
            text_pay.append(
                CompensationRange(
                    source=document.source,
                    source_url=document.final_url,
                    excerpt=match.group()[:1500],
                    observed_at=document.observed_at,
                    minimum=_amount(match["min"]),
                    maximum=_amount(match["max"] or match["min"]),
                    currency=currency,
                    interval={
                        "annual": "year",
                        "yearly": "year",
                        "hourly": "hour",
                        "monthly": "month",
                        "weekly": "week",
                    }[match["period"].lower()],
                    component=component,
                    location=(
                        document.location
                        if us_location(document.location)
                        else ("United States" if match["us"] else document.location)
                    ),
                )
            )
            if component == "unspecified":
                ambiguous_prefixes.add(id(text_pay[-1]))
        except ValidationError:
            continue
    for pay in text_pay:
        if id(pay) in ambiguous_prefixes:
            continue
        line = pay.excerpt
        if re.search(r"\b(?:bonus|equity|stock|stipend|total compensation|OTE)\b", line, re.I):
            pay.component = (
                "total" if re.search(r"total compensation|OTE", line, re.I) else "unspecified"
            )
        elif re.search(r"\b(?:base|salary|hourly pay|pay rate|wage|hourly rate)\b", line, re.I):
            pay.component = "base"
        # Explicit lower/upper bound notation must not become a fixed amount.
        if re.search(r"\b(?:up to|maximum)\b", line, re.I) and pay.minimum == pay.maximum:
            pay.minimum = None
        elif (
            re.search(r"\b(?:from|starting at|at least|minimum)\b", line, re.I)
            and pay.minimum == pay.maximum
        ):
            pay.maximum = None
        pay.source = document.source
    role_title = str(document.role_identity.get("title") or "")
    sales = bool(
        re.search(
            r"\b(?:account executive|sales|business development|bdr|sdr|solutions? consultant)\b",
            role_title,
            re.I,
        )
    )
    structured: list[CompensationRange] = []
    for item in document.structured_pay:
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt or excerpt not in document.text:
            continue
        interval = item.get("interval", "unknown")
        component = item.get("component", "unspecified")
        location = item.get("location") or document.location
        if interval == "unknown" and excerpt.startswith("{") and "title" in excerpt:
            import json

            try:
                raw_range = json.loads(excerpt)
            except ValueError:
                raw_range = None
            if isinstance(raw_range, dict):
                label = str(raw_range.get("title") or "")
                blurb = str(raw_range.get("blurb") or "")
                unit_text = label + "\n" + blurb
                periods = {
                    period
                    for word, period in [
                        ("hourly", "hour"),
                        ("annual", "year"),
                        ("monthly", "month"),
                        ("weekly", "week"),
                    ]
                    if re.search(
                        r"\b"
                        + word
                        + r"\s+(?:(?:base|bachelors?|masters?|phd|intern(?:ship)?)\s+)*"
                        r"(?:salary|pay|rate|wage)\b",
                        unit_text,
                        re.I,
                    )
                }
                if len(periods) == 1:
                    interval = next(iter(periods))
                    ote = bool(
                        re.search(
                            r"\b(?:ote|on[- ]target|commission|variable|total target|incentive)\b",
                            unit_text,
                            re.I,
                        )
                    )
                    if bool(re.search(r"\bbase\b", unit_text, re.I)) or not (sales and ote):
                        component = "base"
                if re.fullmatch(
                    r"(?:[\s:,-]*(?:annual|hourly|monthly|weekly|local|base|salary|pay|wage|"
                    r"rate|range|internship|intern|bachelors?|masters?|phd|new grad|graduate|"
                    r"entry level|compensation)+[\s:,-]*)+",
                    label,
                    re.I,
                ):
                    location = document.location
        try:
            structured.append(
                CompensationRange(
                    source=document.source,
                    source_url=document.final_url,
                    excerpt=item["excerpt"],
                    observed_at=document.observed_at,
                    minimum=item.get("minimum"),
                    maximum=item.get("maximum"),
                    currency=item["currency"],
                    interval=interval,
                    component=component,
                    location=location,
                )
            )
        except (ValidationError, KeyError, InvalidOperation):
            continue
        start = document.text.find(item["excerpt"])
        if start >= 0:
            spans.append(
                {
                    "start": start,
                    "end": start + len(item["excerpt"]),
                    "field": "compensation",
                    "quote": item["excerpt"],
                }
            )
    unique: dict[tuple, CompensationRange] = {}
    for pay in [*structured, *text_pay]:
        key = (pay.minimum, pay.maximum, pay.currency, pay.interval, pay.component, pay.location)
        unique.setdefault(key, pay)
    facts["compensation"] = [item.model_dump(mode="json") for item in list(unique.values())[:20]]
    for item in text_pay:
        start = document.text.find(item.excerpt)
        if start >= 0:
            spans.append(
                {
                    "start": start,
                    "end": start + len(item.excerpt),
                    "field": "compensation",
                    "quote": item.excerpt,
                }
            )
    return facts, spans


def reconcile(documents: list[tuple[Document, dict]]) -> dict:
    """Rebuild from current revisions; removed statements do not linger as current facts."""
    policies: dict[str, PolicyFact] = {}
    pay: list[dict] = []
    restrictions: dict = {}
    for document, facts in sorted(documents, key=lambda pair: pair[0].source_url):
        if not document.complete or document.kind == "company_policy":
            continue
        for key, value in facts["policies"].items():
            fact = PolicyFact.model_validate(value)
            previous = policies.get(key)
            if previous:
                fact = PolicyFact(
                    value=previous.value if previous.value == fact.value else "conflicting",
                    evidence=(previous.evidence + fact.evidence)[:10],
                )
            policies[key] = fact
        for key, value in facts["restrictions"].items():
            restrictions[key] = _merge_restriction(restrictions.get(key), value)
        pay.extend(facts["compensation"])
    # Distinct contradictory overlapping base ranges must remain visible for review.
    conflict = False
    for index, left in enumerate(pay):
        for right in pay[index + 1 :]:
            same_scope = all(
                left.get(key) == right.get(key)
                for key in ("currency", "interval", "component", "location")
            )
            if same_scope and (left.get("minimum"), left.get("maximum")) != (
                right.get("minimum"),
                right.get("maximum"),
            ):
                conflict = True
    details = JobDetails(policies=policies, compensation=pay[:20]).model_dump(mode="json")
    return {**details, "restrictions": restrictions, "pay_conflicting": conflict}


def comparable_amount(pay: dict) -> Decimal | None:
    value = pay.get("maximum") if pay.get("maximum") is not None else pay.get("minimum")
    return Decimal(str(value)) if value is not None else None
