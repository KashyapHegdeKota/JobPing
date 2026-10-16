"""Strict input contracts and transport-neutral evidence documents."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VERSION = "rules-1"
Bucket = Literal["matches", "review", "excluded"]


class Requirements(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    authorization: Literal["cpt", "opt", "stem_opt", "unrestricted"] | None = None
    future_sponsorship: bool = False
    locations: list[str] = Field(default_factory=list, max_length=20)
    internship_hourly: Decimal | None = Field(default=None, ge=0, le=100_000_000)
    new_grad_annual: Decimal | None = Field(default=None, ge=0, le=100_000_000)
    currency: Literal["USD"] = "USD"

    @field_validator("locations")
    @classmethod
    def valid_locations(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("Choose nonempty US work location labels of at most 200 characters")
        return sorted(set(value.strip() for value in values))

    @property
    def configured(self) -> bool:
        return bool(
            self.authorization
            or self.future_sponsorship
            or self.locations
            or self.internship_hourly is not None
            or self.new_grad_annual is not None
        )


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_url: str = Field(max_length=2048)
    final_url: str = Field(max_length=2048)
    kind: Literal["posting", "application", "company_policy"]
    text: str = Field(max_length=100_000)
    observed_at: datetime
    role_identity: dict[str, str | None]
    complete: bool = True
    # Only public job data, not raw forms or applicant values.
    structured_pay: list[dict] = Field(default_factory=list, max_length=20)
    location: str | None = None
    source: str = "employer_page"
