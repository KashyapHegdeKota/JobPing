"""Deterministic identity hashing for job listings."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_WORD_PATTERN = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _normalize_identity_field(value: str) -> str:
    """Return a canonical form suitable for stable identity hashing."""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    without_special_characters = _NON_WORD_PATTERN.sub(" ", normalized)
    without_underscores = without_special_characters.replace("_", " ")
    return _WHITESPACE_PATTERN.sub(" ", without_underscores).strip()


def generate_base_hash(company_name: str, job_title: str) -> str:
    """Generate the SHA-256 identity digest for a company and job title.

    Length-prefixed fields make the composition unambiguous: values such as
    ``("ab", "c")`` and ``("a", "bc")`` cannot produce the same input bytes.
    """
    normalized_company = _normalize_identity_field(company_name)
    normalized_title = _normalize_identity_field(job_title)
    identity = (
        f"{len(normalized_company)}:{normalized_company}"
        f"{len(normalized_title)}:{normalized_title}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
