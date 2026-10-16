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


US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_STATE_NAMES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|"
    "Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
    "Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|"
    "New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
    "Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|"
    "Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
    "District of Columbia"
)
US_METROS = (
    "San Francisco|South San Francisco|SF Bay Area|Bay Area|Silicon Valley|Mountain View|"
    "Palo Alto|Sunnyvale|Santa Clara|Cupertino|Menlo Park|Redwood City|San Jose|"
    "Seattle|Bellevue|Redmond|New York City|NYC"
)
_US = re.compile(
    r"\b(?:USA|U\.?S\.?|United States|"
    + US_STATE_NAMES
    + r"|"
    + US_METROS
    + r")\b|,\s*(?:"
    + "|".join(US_STATES)
    + r")\b",
    re.I,
)
_FOREIGN = re.compile(
    r"\b(?:Canada|Canadian|Toronto|Vancouver|Montreal|Montr[eé]al|Ottawa|Waterloo|Calgary|"
    r"Ontario|Quebec|Qu[eé]bec|British Columbia|Alberta|"
    r"UK|United Kingdom|England|Scotland|London|Cambridge|Oxford|Edinburgh|"
    r"Ireland|Dublin|Germany|Berlin|Munich|France|Paris|Netherlands|Amsterdam|"
    r"Poland|Warsaw|Serbia|Belgrade|Denmark|Aarhus|Copenhagen|Sweden|Stockholm|"
    r"Switzerland|Zurich|Spain|Madrid|Barcelona|Italy|Milan|Rome|Tbilisi|"
    r"India|Bengaluru|Bangalore|Hyderabad|Mumbai|Pune|Delhi|Gurgaon|Chennai|"
    r"Australia|Sydney|Melbourne|Singapore|Mexico|China|Beijing|Shanghai|"
    r"Japan|Tokyo|Korea|Seoul|Brazil|Israel|Tel Aviv|Taiwan|Taipei)\b"
    r"|,\s*(?:BC|ON|QC|AB)\b",
    re.I,
)


def us_location(value: str | None) -> bool:
    return bool(_US.search(value or "")) and not bool(_FOREIGN.search(value or ""))


_TERMS = {
    "cpt": r"\bCPT\b|curricular practical training",
    "opt": r"(?<!STEM )(?<!STEM-)\bOPT\b|(?<!STEM )optional practical training",
    "stem_opt": r"\bSTEM[- ]OPT\b|STEM optional practical training",
    "sponsorship": (
        r"\b(?:visa |immigration |work |employment |H[- ]?1B )?sponsorship\b"
        r"|\bsponsor(?:ing|ed)?\b(?=[^.;]{0,35}\b(?:visa|visas|H[- ]?1B|work|immigration|"
        r"employment|candidates?|applicants?|students?|status|role|position)\b)"
    ),
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
        rf"\b(?:do not|does not|did not|will not|won't|cannot|can't|unable to|not able to)\s+"
        rf"(?:currently\s+|presently\s+|be able to\s+)?"
        rf"(?:accept|support|hire|offer|provide|consider|extend|sponsor)\b[^.;]{{0,70}}{topic}"
        rf"|\b(?:do not|does not|did not|will not|won't|cannot|can't|unable to|not able to)\s+"
        rf"(?:currently\s+|presently\s+|be able to\s+)?{topic}"
        rf"|\b(?:must not|do not|does not|cannot|can't|will not)\s+"
        rf"(?:currently\s+)?(?:require|need)\b[^.;]{{0,50}}{topic}"
        rf"|\bwithout\s+(?:requiring\s+|needing\s+|current or future\s+|employer\s+)?{topic}"
        rf"|\bnot\s+(?:currently\s+)?(?:eligible|available|offered|provided|supported|"
        rf"accepted|permitted)\s+for\b[^.;]{{0,40}}{topic}"
        rf"|\bno\s+(?:current or future\s+)?"
        rf"(?:visa |immigration |work |employment |H[- ]?1B )?{topic}"
        rf"|{topic}[^.;]{{0,40}}\b(?:is\s+|are\s+|will\s+be\s+)?(?:not accepted|not eligible|"
        rf"not available|not supported|not offered|not provided|ineligible|unavailable|"
        rf"not permitted)\b"
        rf"|\b(?:require|requiring|need|needing)\b[^.;]{{0,30}}{topic}\s+"
        rf"(?:are|is|will be)\s+(?:not eligible|ineligible|not considered)\b"
    )
    positive = (
        rf"\b(?:accept|accepts|welcome|support|supports|offer|offers|provide|provides)"
        rf"\b[^.;]{{0,60}}{topic}|{topic}[^.;]{{0,35}}\b"
        rf"(?:welcome|eligible|accepted|available|supported|permitted)\b"
        rf"|\b(?:can|will)\s+(?:currently\s+)?{topic}"
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
        for line in re.split(
            r"[\n;]|(?<=[.!])\s+|,?\s+\b(?:but|however|although|though|while|whereas)\b\s+",
            text,
            flags=re.I,
        ):
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
    # Greenhouse amounts are cents; only explicit unit words in title/blurb establish the period.
    for item in (
        payload.get("pay_input_ranges", [])
        if isinstance(payload.get("pay_input_ranges"), list)
        else []
    ):
        if not isinstance(item, dict):
            continue
        label = str(item.get("title") or "")
        blurb = plain_description(item.get("blurb"))
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
                r"\b" + word + r"\s+(?:(?:base|bachelors?|masters?|phd|intern(?:ship)?)\s+)*"
                r"(?:salary|pay|rate|wage)\b",
                unit_text,
                re.I,
            )
        }
        period = next(iter(periods)) if len(periods) == 1 else "unknown"
        generic_label = bool(
            re.fullmatch(
                r"(?:[\s:,-]*(?:annual|hourly|monthly|weekly|local|base|salary|pay|wage|rate|"
                r"range|internship|intern|bachelors?|masters?|phd|new grad|graduate|"
                r"entry level|compensation)+[\s:,-]*)+",
                label,
                re.I,
            )
        )
        try:
            pay.append(
                CompensationRange(
                    **evidence,
                    excerpt=str(item)[:1500],
                    minimum=_cents(item.get("min_cents")),
                    maximum=_cents(item.get("max_cents")),
                    currency=item.get("currency_type"),
                    interval=period,
                    location=_location(raw) if generic_label else item.get("title"),
                    component="base" if period != "unknown" else "unspecified",
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
                    if not us_location(_location(raw)):
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
