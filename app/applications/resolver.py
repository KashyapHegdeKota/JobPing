"""Deterministic application question resolution."""

from __future__ import annotations

import json
import string
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.candidate.models import CandidateProfile


def normalize_question(value: str) -> str:
    """Lowercase, collapse whitespace, and remove punctuation deterministically."""
    if not isinstance(value, str):
        raise TypeError("question must be a string")
    value = value.casefold().strip()
    value = value.translate(str.maketrans("", "", string.punctuation))
    return " ".join(value.split())


ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "given name", "legal first name"),
    "last_name": ("last name", "surname", "family name", "legal last name"),
    "email": ("email", "email address", "e mail"),
    "phone": ("phone", "phone number", "telephone", "mobile number"),
    "location": ("location", "current location", "city", "where do you live"),
    "school": ("school", "university", "college", "educational institution"),
    "degree": ("degree", "degree type"),
    "major": ("major", "field of study", "course of study"),
    "linkedin": ("linkedin", "linkedin profile", "linkedin url"),
    "github": ("github", "github profile", "github url"),
    "portfolio": ("portfolio", "portfolio url", "personal website"),
    "work_authorization": (
        "authorized to work",
        "legally authorized to work",
        "authorization to work",
        "work authorization",
    ),
    "sponsorship": (
        "require sponsorship",
        "need sponsorship",
        "future sponsorship",
        "visa sponsorship",
        "sponsorship",
    ),
    "relocate": ("relocate", "willing to relocate", "open to relocation"),
    "graduation_date": ("graduation date", "expected graduation", "graduation month"),
    "gpa": ("gpa", "grade point average"),
}

PROTECTED_CATEGORIES = frozenset(
    {
        "citizenship",
        "work_authorization",
        "sponsorship",
        "security_clearance",
        "criminal_history",
        "gpa",
        "graduation_date",
    }
)


class AnswerResolution(BaseModel):
    """Safe resolution returned to an application agent."""

    model_config = ConfigDict(extra="forbid")

    matched: bool
    answer: str | None = None
    source: str | None = None
    generation_allowed: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    relevant_stories: list[str] = Field(default_factory=list)
    requires_review: bool = False


def _load_json(value: Mapping[str, Any] | Path | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Path):
        payload = json.loads(value.read_text(encoding="utf-8"))
    else:
        payload = dict(value)
    if not isinstance(payload, dict):
        raise ValueError("answer configuration must be a JSON object")
    return payload


class AnswerResolver:
    """Resolve candidate facts and answer-bank entries without model calls."""

    def __init__(
        self,
        profile: CandidateProfile | None = None,
        *,
        answers: Mapping[str, Any] | Path | None = None,
        stories: Mapping[str, Any] | Path | None = None,
        rules: Mapping[str, Any] | Path | None = None,
    ) -> None:
        self.profile = profile
        self.answers = _load_json(answers)
        self.stories = _load_json(stories).get("stories", {})
        configured_rules = _load_json(rules)
        self.generation_allowed = frozenset(
            normalize_question(str(item).replace("_", " ")).replace(" ", "_")
            for item in configured_rules.get(
                "generation_allowed",
                (
                    "why_company",
                    "why_role",
                    "project_description",
                    "technical_challenge",
                    "leadership",
                    "failure",
                ),
            )
        )
        self.never_invent = frozenset(
            normalize_question(str(item).replace("_", " ")).replace(" ", "_")
            for item in configured_rules.get("never_invent", PROTECTED_CATEGORIES)
        )

    def _fact_values(self) -> dict[str, str | None]:
        if self.profile is None:
            return {}
        p = self.profile
        return {
            "first_name": p.personal.first_name,
            "last_name": p.personal.last_name,
            "email": str(p.personal.email),
            "phone": p.personal.phone,
            "location": p.personal.location,
            "school": p.education.school,
            "degree": p.education.degree,
            "major": p.education.major,
            "graduation_date": p.education.graduation_date,
            "gpa": p.education.gpa,
            "linkedin": str(p.links.linkedin) if p.links.linkedin else None,
            "github": str(p.links.github) if p.links.github else None,
            "portfolio": str(p.links.portfolio) if p.links.portfolio else None,
            "work_authorization": "Yes" if p.work_authorization.authorized_us else "No",
            "sponsorship": "Yes" if p.work_authorization.requires_sponsorship else "No",
        }

    def _category(self, normalized: str) -> str | None:
        for category, aliases in ALIASES.items():
            if any(
                alias == normalized or alias in normalized
                for alias in map(normalize_question, aliases)
            ):
                return category
        return None

    def resolve(self, question: str) -> AnswerResolution:
        normalized = normalize_question(question)
        category = self._category(normalized)
        facts = self._fact_values()
        if category and facts.get(category) is not None:
            return AnswerResolution(
                matched=True,
                answer=facts[category],
                source="candidate_profile",
                generation_allowed=False,
                confidence=1.0,
            )

        work_authorization = self.answers.get("work_authorization", {})
        availability = self.answers.get("availability", {})
        structured_answers = {
            "work_authorization": (
                work_authorization.get("authorized_us")
                if isinstance(work_authorization, dict)
                else None
            ),
            "sponsorship": (
                work_authorization.get("requires_sponsorship")
                if isinstance(work_authorization, dict)
                else None
            ),
            "relocate": (availability.get("relocate") if isinstance(availability, dict) else None),
        }
        if category and structured_answers.get(category) is not None:
            return AnswerResolution(
                matched=True,
                answer=str(structured_answers[category]),
                source="answer_bank",
                generation_allowed=False,
                confidence=1.0,
            )

        configured = self.answers.get("application_answers", [])
        for entry in configured:
            patterns = entry.get("patterns", []) if isinstance(entry, dict) else []
            if any(
                normalize_question(pattern) in normalized
                or set(normalize_question(pattern).split()).issubset(set(normalized.split()))
                for pattern in patterns
            ):
                return AnswerResolution(
                    matched=True,
                    answer=str(entry.get("answer", "")),
                    source="answer_bank",
                    generation_allowed=False,
                    confidence=1.0,
                )

        configured_protected = any(
            phrase.replace("_", " ") in normalized for phrase in self.never_invent
        )
        protected = (
            category in PROTECTED_CATEGORIES
            or configured_protected
            or any(
                term in normalized
                for term in ("citizenship", "security clearance", "criminal history", "visa")
            )
        )
        if protected:
            return AnswerResolution(matched=False, generation_allowed=False, requires_review=True)

        key = (category or normalized).replace(" ", "_")
        allowed_phrases = {item.replace("_", " ") for item in self.generation_allowed}
        allowed = key in self.generation_allowed or any(
            phrase in normalized for phrase in allowed_phrases
        )
        relevant = sorted(str(name) for name in self.stories) if allowed else []
        return AnswerResolution(
            matched=False,
            generation_allowed=allowed,
            relevant_stories=relevant,
            requires_review=not allowed,
        )


def lookup_answer(question: str, resolver: AnswerResolver) -> AnswerResolution:
    """Functional convenience wrapper used by MCP adapters and tests."""
    return resolver.resolve(question)


__all__ = [
    "ALIASES",
    "AnswerResolution",
    "AnswerResolver",
    "lookup_answer",
    "normalize_question",
]
