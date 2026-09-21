"""Deterministic identity hashing for job listings."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import ParseResult, SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

_NON_WORD_PATTERN = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")

_TRACKING_PARAMETER_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "referrer",
        "yclid",
    }
)
_TRACKING_PARAMETER_PREFIXES = ("utm_",)
_ATS_HOST_ALIASES = {
    "boards.greenhouse.io": "job-boards.greenhouse.io",
    "job-boards.greenhouse.io": "job-boards.greenhouse.io",
    "jobs.eu.lever.co": "jobs.lever.co",
    "jobs.lever.co": "jobs.lever.co",
}
_KNOWN_REDIRECT_HOSTS = frozenset(
    {
        "applyguy.ai",
        "www.applyguy.ai",
        "simplify.jobs",
        "www.simplify.jobs",
    }
)


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


def canonicalize_apply_url(apply_url: str) -> str:
    """Canonicalize an application URL without discarding application state.

    Tracking parameters are removed because they do not identify a different
    application. Known ATS host aliases are also folded so a Greenhouse or
    Lever URL cannot alternate the stored content state as sources are polled.
    Unknown query parameters, paths, and fragments remain significant.
    """
    normalized_url = unicodedata.normalize("NFKC", apply_url).strip()
    parts = urlsplit(normalized_url)

    # Schemes and host names are case-insensitive. Paths, queries, fragments,
    # credentials, explicit ports, and their ordering are deliberately retained:
    # ATS providers may encode materially different application state in them.
    if parts.hostname is None:
        return urlunsplit(_canonical_url_parts(parts))

    user_info = ""
    if parts.username is not None:
        user_info = parts.username
        if parts.password is not None:
            user_info += f":{parts.password}"
        user_info += "@"

    host = _ATS_HOST_ALIASES.get(parts.hostname.lower(), parts.hostname.lower())
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{user_info}{host}{port}"
    return urlunsplit(_canonical_url_parts(parts, netloc=netloc))


def _canonical_url_parts(parts: SplitResult, *, netloc: str | None = None) -> ParseResult:
    """Return normalized URL parts while removing only known tracking keys."""
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_PARAMETER_NAMES
            and not any(
                key.casefold().startswith(prefix) for prefix in _TRACKING_PARAMETER_PREFIXES
            )
        ],
        doseq=True,
    )
    return SplitResult(
        parts.scheme.lower(),
        parts.netloc if netloc is None else netloc,
        parts.path.rstrip("/") or ("/" if parts.path else ""),
        query,
        parts.fragment,
    )


def _url_preference(apply_url: str) -> int:
    """Rank URLs from direct ATS/employer links to source redirects."""
    host = (urlsplit(apply_url).hostname or "").casefold().removeprefix("www.")
    if host in _KNOWN_REDIRECT_HOSTS:
        return 1
    if host.endswith("greenhouse.io") or host.endswith("lever.co"):
        return 4
    if host.endswith("myworkdayjobs.com") or host.endswith("workday.com"):
        return 4
    if host.endswith("amazon.jobs") or host.endswith("ashbyhq.com"):
        return 4
    return 3


def choose_canonical_apply_url(current_url: str, incoming_url: str) -> str:
    """Choose the better URL while retaining a deterministic existing winner.

    Equal-ranked URLs are intentionally not compared lexically: preserving the
    current direct link avoids source polling order changing a user's saved
    application destination.
    """
    current = canonicalize_apply_url(current_url)
    incoming = canonicalize_apply_url(incoming_url)
    if not current:
        return incoming
    if not incoming:
        return current
    return incoming if _url_preference(incoming) > _url_preference(current) else current


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
        canonicalize_apply_url(apply_url),
        _normalize_location(location),
        "true" if is_closed else "false",
    )
    state = "".join(f"{len(field)}:{field}" for field in fields)
    return hashlib.sha256(state.encode("utf-8")).hexdigest()
