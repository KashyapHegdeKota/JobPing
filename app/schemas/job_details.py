"""Evidence-backed discovery details; these never determine legal eligibility."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

PolicyValue = Literal["allowed", "denied", "conditional", "conflicting", "unknown"]
PolicyKind = Literal["cpt", "opt", "stem_opt", "sponsorship", "future_sponsorship"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=100)
    source_url: HttpUrl
    excerpt: str = Field(min_length=1, max_length=1500)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include timezone")
        return value


class PolicyFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: PolicyValue = "unknown"
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def supported_value(self) -> "PolicyFact":
        if self.value != "unknown" and not self.evidence:
            raise ValueError("a stated policy requires source evidence")
        return self


class CompensationRange(Evidence):
    minimum: Decimal | None = Field(default=None, ge=0, le=100_000_000)
    maximum: Decimal | None = Field(default=None, ge=0, le=100_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    interval: Literal["hour", "month", "year", "week", "unknown"] = "unknown"
    location: str | None = Field(default=None, max_length=500)
    component: Literal["base", "total", "unspecified"] = "unspecified"

    @model_validator(mode="after")
    def valid_range(self) -> "CompensationRange":
        if self.minimum is None and self.maximum is None:
            raise ValueError("at least one pay bound is required")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("pay minimum exceeds maximum")
        return self


class JobDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policies: dict[PolicyKind, PolicyFact] = Field(default_factory=dict)
    compensation: list[CompensationRange] = Field(default_factory=list, max_length=20)


class EmployerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "h1b_filings", "h1b_approvals", "cpt_history", "opt_history", "stem_opt_history", "e_verify"
    ]
    year: int = Field(ge=2000, le=2100)
    count: int = Field(ge=1, le=100_000_000)
    employer_name: str = Field(min_length=1, max_length=255)
    source_url: HttpUrl
    observed_at: datetime

    _aware = field_validator("observed_at")(Evidence.aware.__func__)

    @model_validator(mode="after")
    def official_source(self) -> "EmployerRecord":
        hosts = {
            "h1b_filings": "dol.gov",
            "h1b_approvals": "uscis.gov",
            "cpt_history": "ice.gov",
            "opt_history": "ice.gov",
            "stem_opt_history": "ice.gov",
            "e_verify": "e-verify.gov",
        }
        host = self.source_url.host or ""
        expected = hosts[self.kind]
        if self.source_url.scheme != "https" or not (
            host == expected or host.endswith("." + expected)
        ):
            raise ValueError("employer history requires the corresponding official HTTPS source")
        return self
