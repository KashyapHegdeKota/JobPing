"""Deterministic identity hashing for job listings."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import SplitResult, urlsplit, urlunsplit

_NON_WORD_PATTERN = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


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


def _canonicalize_base_hash(base_hash: str) -> str:
    """Validate and canonicalize a hexadecimal SHA-256 digest."""
    canonical_hash = base_hash.strip().lower()
    if _SHA256_PATTERN.fullmatch(canonical_hash) is None:
        raise ValueError("base_hash must be a 64-character hexadecimal SHA-256 digest")
    return canonical_hash


def _normalize_url(apply_url: str) -> str:
    """Normalize safe URL components without discarding application-link state."""
    normalized_url = unicodedata.normalize("NFKC", apply_url).strip()
    parts = urlsplit(normalized_url)

    # Schemes and host names are case-insensitive. Paths, queries, fragments,
    # credentials, explicit ports, and their ordering are deliberately retained:
    # ATS providers may encode materially different application state in them.
    if parts.hostname is None:
        return urlunsplit(
            SplitResult(parts.scheme.lower(), parts.netloc, parts.path, parts.query, parts.fragment)
        )

    user_info = ""
    if parts.username is not None:
        user_info = parts.username
        if parts.password is not None:
            user_info += f":{parts.password}"
        user_info += "@"

    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{user_info}{host}{port}"
    return urlunsplit(
        SplitResult(parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment)
    )


def _normalize_location(location: str) -> str:
    """Normalize human-readable location text while preserving punctuation."""
    normalized = unicodedata.normalize("NFKC", location).casefold().strip()
    return _WHITESPACE_PATTERN.sub(" ", normalized)


def generate_content_hash(base_hash: str, apply_url: str, location: str, is_closed: bool) -> str:
    """Generate a SHA-256 digest representing a job posting's current state.

    Every field is length-prefixed so composition cannot be ambiguous. The
    boolean uses a stable textual representation independent of Python's
    display conventions.
    """
    fields = (
        _canonicalize_base_hash(base_hash),
        _normalize_url(apply_url),
        _normalize_location(location),
        "true" if is_closed else "false",
    )
    state = "".join(f"{len(field)}:{field}" for field in fields)
    return hashlib.sha256(state.encode("utf-8")).hexdigest()
