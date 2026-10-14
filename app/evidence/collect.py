"""Exact-role ATS and reviewed employer-page adapters."""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urljoin, urlsplit

import httpx

from app.evidence.fetch import Fetcher, FetchError, Page
from app.evidence.schemas import Document
from app.services.hasher import canonicalize_apply_url
from app.services.source_identity import stable_posting_identity

ROLE_IDS = {"content", "application_form", "application-form"}
ROLE_CLASSES = {
    "posting-description",
    "posting-additional",
    "application-form",
    "job__description",
    "job__pay-ranges",
    "application--form",
}
ROLE_SELECTOR = ",".join(
    [
        *("#" + item for item in sorted(ROLE_IDS)),
        *("." + item for item in sorted(ROLE_CLASSES)),
        '[itemprop="description"]',
    ]
)


class PublicHTML(HTMLParser):
    """Only public prose and JSON-LD; form values and hidden nodes are never collected."""

    def __init__(self, url: str, *, role_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.url = url
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.structured: list[dict] = []
        self.stack: list[bool] = []
        self.tags: list[str] = []
        self.scopes: list[bool] = []
        self.role_only = role_only
        self.ld: list[str] | None = None
        self.link: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self.ld = []
        hidden = (
            bool(self.stack and self.stack[-1])
            or tag
            in {"script", "style", "noscript", "textarea", "select", "nav", "footer", "aside"}
            or bool(
                re.search(
                    r"related|recommended|similar.jobs|other.jobs",
                    (attributes.get("id") or "") + " " + (attributes.get("class") or ""),
                    re.I,
                )
            )
            or "hidden" in attributes
            or attributes.get("aria-hidden") == "true"
            or bool(
                re.search(
                    r"display\s*:\s*none|visibility\s*:\s*hidden",
                    attributes.get("style") or "",
                    re.I,
                )
            )
        )
        scoped = bool(self.scopes and self.scopes[-1]) or (
            attributes.get("id") in ROLE_IDS
            or bool(set((attributes.get("class") or "").split()) & ROLE_CLASSES)
            or attributes.get("itemprop") == "description"
        )
        if tag not in {
            "br",
            "input",
            "img",
            "meta",
            "link",
            "hr",
            "source",
            "wbr",
            "area",
            "embed",
            "param",
            "track",
            "col",
            "base",
        }:
            self.stack.append(hidden)
            self.tags.append(tag)
            self.scopes.append(scoped)
        visible = not hidden and (not self.role_only or scoped)
        if tag in {"p", "br", "li", "div", "label", "h1", "h2", "h3"} and visible:
            self.parts.append("\n")
            if tag == "label":
                self.parts.append("Question: ")
        if tag == "a" and visible and attributes.get("href"):
            self.link = (urljoin(self.url, attributes["href"]), [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.ld is not None:
            try:
                value = json.loads("".join(self.ld))
                self._jobs(value)
            except (ValueError, TypeError):
                pass
            self.ld = None
        if tag == "a" and self.link:
            self.links.append((self.link[0], "".join(self.link[1])))
            self.link = None
        if tag in self.tags:
            index = len(self.tags) - 1 - self.tags[::-1].index(tag)
            del self.tags[index:]
            del self.stack[index:]
            del self.scopes[index:]
        if tag in {"p", "li", "div", "label"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ld is not None:
            self.ld.append(data)
        if (not self.stack or not self.stack[-1]) and (
            not self.role_only or self.scopes and self.scopes[-1]
        ):
            self.parts.append(data)
            if self.link:
                self.link[1].append(data)

    def _jobs(self, value: object) -> None:
        if isinstance(value, list):
            for item in value[:50]:
                self._jobs(item)
        elif isinstance(value, dict):
            if value.get("@type") == "JobPosting" and len(self.structured) < 20:
                organization = value.get("hiringOrganization")
                salary = value.get("baseSalary")
                quantity = salary.get("value") if isinstance(salary, dict) else None
                self.structured.append(
                    {
                        **{key: value.get(key) for key in ("url", "title", "description")},
                        "hiringOrganization": (
                            {"name": organization.get("name")}
                            if isinstance(organization, dict)
                            else None
                        ),
                        "baseSalary": (
                            {
                                "currency": salary.get("currency"),
                                "value": {
                                    key: quantity.get(key)
                                    for key in ("minValue", "maxValue", "value", "unitText")
                                },
                            }
                            if isinstance(quantity, dict)
                            else None
                        ),
                    }
                )
            if isinstance(value.get("@graph"), list):
                self._jobs(value["@graph"])

    @property
    def text(self) -> str:
        return "\n".join(
            " ".join(part.split()) for part in "".join(self.parts).splitlines() if part.strip()
        )


def prose(value: object) -> str:
    parser = PublicHTML("https://example.invalid/")
    parser.feed(html.unescape(value) if isinstance(value, str) else "")
    return parser.text


def matching_posting(items: list[dict], target: dict, url: str) -> dict | None:
    """Only one exact URL, hiring entity and title may own generic role facts."""
    matches = [
        item
        for item in items
        if isinstance(item.get("url"), str)
        and canonicalize_apply_url(item["url"]) == canonicalize_apply_url(url)
        and (
            target.get("namespace")
            or isinstance(item.get("hiringOrganization"), dict)
            and item["hiringOrganization"].get("name") == target["company"]
        )
        and isinstance(item.get("title"), str)
        and " ".join(item["title"].casefold().split())
        == " ".join((target.get("title") or "").casefold().split())
        and isinstance(item.get("description"), str)
    ]
    return matches[0] if len(matches) == 1 else None


def role_identity(url: str) -> tuple[str, str] | None:
    return stable_posting_identity(source=None, source_id=None, apply_url=url, payload={})


def verified_url(url: str, target: dict, reviewed: dict[str, str]) -> bool:
    identity = role_identity(url)
    expected = (target.get("namespace"), target.get("external_id"))
    if identity is not None:
        return identity == expected
    host = (urlsplit(url).hostname or "").lower()
    return reviewed.get(host) == target["company"] and canonicalize_apply_url(
        url
    ) == canonicalize_apply_url(target["url"])


def pay_from_jsonld(item: dict) -> list[dict]:
    salary = item.get("baseSalary")
    if not isinstance(salary, dict):
        return []
    value = salary.get("value")
    if not isinstance(value, dict):
        return []
    period = {"HOUR": "hour", "YEAR": "year", "MONTH": "month", "WEEK": "week"}.get(
        str(value.get("unitText")).upper(), "unknown"
    )
    support = {
        "currency": salary.get("currency"),
        "value": {key: value.get(key) for key in ("minValue", "maxValue", "value", "unitText")},
    }
    return [
        {
            "minimum": value.get("minValue", value.get("value")),
            "maximum": value.get("maxValue", value.get("value")),
            "currency": salary.get("currency"),
            "interval": period,
            "component": "base",
            "excerpt": json.dumps(support, sort_keys=True)[:1500],
        }
    ]


class CollectionError(FetchError):
    def __init__(self, code: str, documents: list[Document]) -> None:
        super().__init__(code)
        self.documents = documents


class Collector:
    def __init__(
        self,
        fetcher: Fetcher,
        *,
        reviewed: dict[str, str] | None = None,
        renderer_url: str | None = None,
        renderer_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.reviewed = reviewed or {}
        self.renderer_url = renderer_url
        self.renderer_client = renderer_client

    async def collect(self, target: dict) -> list[Document]:
        documents: list[Document] = []
        try:
            return await self._collect(target, documents)
        except FetchError as exc:
            raise CollectionError(str(exc), documents) from None

    async def _collect(self, target: dict, documents: list[Document]) -> list[Document]:
        now = datetime.now(UTC)
        namespace = target.get("namespace") or ""
        provider, _, tenant = namespace.partition(":")
        if provider in {"greenhouse", "lever"}:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", tenant):
                raise FetchError("identity_unavailable")
            identifier = quote(target["external_id"], safe="")
            if provider == "greenhouse":
                url = f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs/{identifier}?questions=true&pay_transparency=true"
            else:
                region = "api.eu.lever.co" if "eu.lever.co" in target["url"] else "api.lever.co"
                url = f"https://{region}/v0/postings/{tenant}/{identifier}?mode=json"
            response = await self.fetcher.get(url)
            try:
                body = json.loads(response.body)
            except ValueError:
                raise FetchError("invalid_source_json") from None
            if not isinstance(body, dict) or str(body.get("id")) != target["external_id"]:
                raise FetchError("identity_mismatch")
            title = body.get("title") if provider == "greenhouse" else body.get("text")
            if target.get("title") and (
                not isinstance(title, str)
                or " ".join(title.casefold().split())
                != " ".join(target["title"].casefold().split())
            ):
                raise FetchError("identity_mismatch")
            hosted = body.get("absolute_url") if provider == "greenhouse" else body.get("hostedUrl")
            if not isinstance(hosted, str) or not (
                role_identity(hosted) == (namespace, target["external_id"])
                or canonicalize_apply_url(hosted) == canonicalize_apply_url(target["url"])
            ):
                raise FetchError("identity_mismatch")
            pieces = (
                [body.get("content")]
                if provider == "greenhouse"
                else [
                    body.get("descriptionPlain") or body.get("description"),
                    body.get("additionalPlain") or body.get("additional"),
                    body.get("salaryDescriptionPlain") or body.get("salaryDescription"),
                ]
            )
            for item in body.get("lists", []) if isinstance(body.get("lists"), list) else []:
                if isinstance(item, dict):
                    pieces.append(item.get("content"))
            text = "\n".join(prose(piece) for piece in pieces)
            pay: list[dict] = []
            for item in body.get("pay_input_ranges", []) if provider == "greenhouse" else []:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("title") or "")
                blurb = prose(item.get("blurb"))
                unit_text = label + "\n" + blurb
                periods = {
                    period
                    for word, period in [
                        ("hourly", "hour"),
                        ("annual", "year"),
                        ("monthly", "month"),
                        ("weekly", "week"),
                    ]
                    if re.search(r"\b" + word + r"\s+base\s+(?:salary|pay|rate)\b", unit_text, re.I)
                }
                period = next(iter(periods)) if len(periods) == 1 else "unknown"
                generic_label = bool(
                    re.fullmatch(
                        r"(?:Annual Base Salary Range:?|Hourly Base Pay Range:?|"
                        r"Internship|Base Salary Range:?)",
                        label,
                        re.I,
                    )
                )
                evidence_item = {
                    key: item.get(key)
                    for key in ("min_cents", "max_cents", "currency_type", "title")
                }
                evidence_item["blurb"] = blurb[:900]
                excerpt = json.dumps(evidence_item, sort_keys=True)[:1500]
                text += "\n" + excerpt
                pay.append(
                    {
                        "minimum": (
                            str(Decimal(str(item["min_cents"])) / 100)
                            if item.get("min_cents") is not None
                            else None
                        ),
                        "maximum": (
                            str(Decimal(str(item["max_cents"])) / 100)
                            if item.get("max_cents") is not None
                            else None
                        ),
                        "currency": item.get("currency_type"),
                        "interval": period,
                        "component": "base" if period != "unknown" else "unspecified",
                        "location": target["location"] if generic_label else item.get("title"),
                        "excerpt": excerpt,
                    }
                )
            salary = body.get("salaryRange")
            if provider == "lever" and isinstance(salary, dict):
                pay.append(
                    {
                        "minimum": salary.get("min"),
                        "maximum": salary.get("max"),
                        "currency": salary.get("currency"),
                        "interval": {"hourly": "hour", "yearly": "year", "annual": "year"}.get(
                            str(salary.get("interval")), salary.get("interval", "unknown")
                        ),
                        "excerpt": json.dumps(salary, sort_keys=True)[:1500],
                    }
                )
                text += "\n" + pay[-1]["excerpt"]
            documents.append(
                Document(
                    source_url=url,
                    final_url=response.url,
                    kind="posting",
                    text=text,
                    observed_at=now,
                    role_identity=target,
                    source=provider,
                    location=target["location"],
                    structured_pay=pay,
                )
            )
            if provider == "greenhouse":
                labels = [
                    prose(item.get("label"))
                    for item in body.get("questions", [])
                    if isinstance(item, dict)
                ]
                documents[0].text += "\n" + "\n".join("Question: " + label for label in labels)
            page_url = hosted if provider == "greenhouse" else body.get("applyUrl") or hosted
            # Only the exact posting's known /apply suffix may supplement a Lever role.
            if role_identity(page_url) != (namespace, target["external_id"]) and page_url != hosted:
                raise FetchError("identity_mismatch")
        else:
            page_url = target["url"]
            if not verified_url(page_url, target, self.reviewed):
                raise FetchError("employer_not_reviewed")
        page = await self.fetcher.get(page_url)
        expected_host = (urlsplit(page_url).hostname or "").lower()
        same_role = (
            role_identity(page.url) == (namespace, target.get("external_id"))
            if namespace
            else False
        )
        branded_identity = bool(
            provider == "greenhouse"
            and (urlsplit(page.url).hostname or "").removeprefix("www.")
            == expected_host.removeprefix("www.")
            and parse_qs(urlsplit(page.url).query).get("gh_jid") == [target["external_id"]]
        )
        if (
            not same_role
            and not branded_identity
            and canonicalize_apply_url(page.url) != canonicalize_apply_url(page_url)
        ):
            raise FetchError("identity_mismatch")
        parser = self.parse(page, role_only=bool(namespace))
        text = parser.text
        if self.barrier(text):
            raise FetchError("verification_barrier")
        if (
            (len(text) < 100 or not parser.structured)
            and self.renderer_url
            and self.renderer_client
        ):
            response = await self.renderer_client.post(
                self.renderer_url + "/render", json={"url": page.url}, timeout=45
            )
            response.raise_for_status()
            rendered = response.json()
            if rendered.get("url") != page.url:
                raise FetchError("identity_mismatch")
            text = rendered["text"]
            parser.structured = rendered.get("structured", [])
        generic = matching_posting(parser.structured, target, page_url)
        if not namespace:
            if generic is None:
                raise FetchError("role_scope_unverified")
            text = prose(generic["description"])
        elif generic:
            text = prose(generic["description"]) + "\n" + text
        if len(text) > 100_000:
            raise FetchError("document_too_large")
        if self.barrier(text):
            raise FetchError("verification_barrier")
        if len(text) < 100:
            raise FetchError("role_scope_unverified")
        pay = []
        for item in parser.structured:
            if not namespace and item is not generic:
                continue
            item_url = item.get("url")
            if isinstance(item_url, str) and canonicalize_apply_url(
                item_url
            ) == canonicalize_apply_url(page_url):
                organization = item.get("hiringOrganization", {})
                if not namespace and (
                    not isinstance(organization, dict)
                    or organization.get("name") != target["company"]
                ):
                    continue
                pay.extend(pay_from_jsonld(item))
        for item in pay:
            text += "\n" + item["excerpt"]
        documents.append(
            Document(
                source_url=page_url,
                final_url=page.url,
                kind="application" if provider == "lever" else "posting",
                text=text,
                observed_at=now,
                role_identity=target,
                source=provider or "employer_page",
                location=target["location"],
                structured_pay=pay[:20],
            )
        )
        for link, label in parser.links:
            if len(documents) >= 3:
                break
            if (
                (urlsplit(link).hostname or "").lower() == expected_host
                and re.search(r"visa|immigration|sponsorship|compensation|pay policy", label, re.I)
                and canonicalize_apply_url(link) != canonicalize_apply_url(page_url)
            ):
                policy = await self.fetcher.get(link)
                if (urlsplit(policy.url).hostname or "").lower() != expected_host:
                    raise FetchError("identity_mismatch")
                documents.append(
                    Document(
                        source_url=link,
                        final_url=policy.url,
                        kind="company_policy",
                        text=self.parse(policy).text,
                        observed_at=now,
                        role_identity=target,
                    )
                )
        return documents

    @staticmethod
    def parse(page: Page, *, role_only: bool = False) -> PublicHTML:
        if "html" not in page.content_type:
            raise FetchError("unsupported_document")
        parser = PublicHTML(page.url, role_only=role_only)
        parser.feed(page.body.decode("utf-8", errors="replace"))
        return parser

    @staticmethod
    def barrier(text: str) -> bool:
        return bool(
            re.search(
                r"verify (?:you are|you're) human|complete (?:the )?captcha|"
                r"sign in to (?:apply|continue)|log in to (?:apply|continue)|access denied",
                text,
                re.I,
            )
        )
