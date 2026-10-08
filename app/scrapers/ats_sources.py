"""Configured direct ATS boards and conservative title eligibility."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from app.schemas.job import JobType, RawJobPayload
from app.scrapers.base import BaseScraper
from app.scrapers.greenhouse import GreenhouseScraper
from app.scrapers.lever import LeverScraper

ATS_DOMAINS = {"boards.greenhouse.io": "greenhouse", "api.lever.co": "lever"}
_YEAR = re.compile(r"(?<!\d)20\d{2}(?!\d)")
_INTERN = re.compile(r"\b(?:intern(?:ship)?s?|co[ -]?op)\b", re.IGNORECASE)
_GRAD = re.compile(
    r"\b(?:(?:new|recent|university|college)[ -]+grad(?:uate)?s?"
    r"|graduate[ -]+(?:engineer|developer|analyst|program|programme|trainee)s?)\b",
    re.IGNORECASE,
)
_SENIOR = re.compile(r"\b(?:senior|sr\.?|staff|principal|director|head|lead)\b", re.IGNORECASE)


class ATSSource(BaseModel):
    """One board token, company identity and explicitly configured season."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    provider: Literal["greenhouse", "lever"]
    token: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    company: str = Field(min_length=1, max_length=255)
    season: Literal[2026, 2027]
    job_types: tuple[JobType, ...] = (JobType.INTERNSHIP, JobType.NEW_GRAD)
    allow_undated: StrictBool = False

    @model_validator(mode="after")
    def unique_types(self) -> ATSSource:
        if not self.job_types or len(set(self.job_types)) != len(self.job_types):
            raise ValueError("job_types must be nonempty and unique")
        return self

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.token}"

    def eligible_type(self, title: str) -> JobType | None:
        """Title evidence only; descriptions and posting dates cannot set a season."""
        title = unicodedata.normalize("NFKC", title).replace("–", "-").replace("—", "-")
        if _SENIOR.search(title):
            return None
        years = {int(match) for match in _YEAR.findall(title)}
        if years:
            if years != {self.season}:
                return None
        elif not self.allow_undated:
            return None
        internship = bool(_INTERN.search(title))
        graduate = bool(_GRAD.search(title))
        if internship == graduate:
            return None
        category = JobType.INTERNSHIP if internship else JobType.NEW_GRAD
        return category if category in self.job_types else None


class ATSSources(BaseModel):
    """Strict registry; one entry per provider/tenant prevents duplicate polling."""

    model_config = ConfigDict(extra="forbid")
    sources: tuple[ATSSource, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_boards(self) -> ATSSources:
        identities = {(item.provider, item.token.casefold()) for item in self.sources}
        if len(identities) != len(self.sources):
            raise ValueError("duplicate ATS provider/token")
        return self


def load_ats_sources(path: Path | None) -> tuple[ATSSource, ...]:
    """Read bounded local configuration without creating network resources."""
    if path is None:
        return ()
    try:
        with path.open("rb") as stream:
            data = stream.read(1_048_577)
    except OSError as exc:
        raise ValueError(f"Cannot read ATS sources file: {path}") from exc
    if len(data) > 1_048_576:
        raise ValueError("ATS sources file exceeds 1 MiB")
    try:
        return ATSSources.model_validate_json(data).sources
    except ValidationError as exc:
        # Configuration errors must not echo arbitrary untrusted file contents.
        raise ValueError("Invalid ATS sources file; see the documented registry schema") from exc


class ConfiguredATSScraper(BaseScraper):
    """Fetch once, retain token provenance and emit only configured eligible roles."""

    def __init__(self, source: ATSSource, *, client: httpx.AsyncClient) -> None:
        super().__init__(scraper_name=source.provider, company=source.company, client=client)
        self.config = source
        self.fetched_count = 0
        self.filtered_count = 0
        self._delegate = (
            GreenhouseScraper(company=source.token, client=client)
            if source.provider == "greenhouse"
            else LeverScraper(company=source.token, client=client)
        )

    async def fetch_jobs(self) -> list[RawJobPayload]:
        self.fetched_count = 0
        self.filtered_count = 0
        rows = await self._delegate.run()
        self.fetched_count = len(rows)
        eligible: list[RawJobPayload] = []
        for row in rows:
            category = self.config.eligible_type(row.title or "")
            if category is None:
                self.filtered_count += 1
                continue
            if isinstance(self._delegate, GreenhouseScraper):
                row = await self._delegate.enrich_pay(row)
            eligible.append(
                row.model_copy(
                    update={
                        "company": self.config.company,
                        "season": self.config.season,
                        "job_type": category.value,
                    }
                )
            )
        return eligible

    async def aclose(self) -> None:
        try:
            await self._delegate.aclose()
        finally:
            await super().aclose()
