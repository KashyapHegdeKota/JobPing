"""Conservative stable posting identity extraction from ATS provenance."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

from app.services.hasher import canonicalize_apply_url

_DIRECT_ATS_SOURCES = frozenset({"greenhouse", "lever", "workday", "ashby", "amazon", "meta"})


def stable_posting_identity(
    *, source: str | None, source_id: str | None, apply_url: str, payload: dict[str, object]
) -> tuple[str, str] | None:
    """Return a provider/tenant-scoped ATS key when the evidence is stable.

    Feed-local IDs from ApplyGuy and Simplify's commit/path IDs are deliberately
    excluded. A direct ATS URL can still expose the durable requisition identity.
    """
    url_identity = _identity_from_url(apply_url)
    normalized_source = (source or "").strip().casefold()
    if url_identity is not None:
        return url_identity
    if normalized_source not in _DIRECT_ATS_SOURCES or not source_id:
        return None

    if source_id.startswith(("http://", "https://")):
        return None
    external_id = str(payload.get("id") or source_id).strip()
    if normalized_source == "greenhouse":
        match = re.fullmatch(r"greenhouse:([^:]+):([^:]+)", external_id, re.IGNORECASE)
        if match:
            tenant, external_id = match.groups()
        else:
            tenant = _tenant_from_url(apply_url)
    else:
        tenant = _tenant_from_url(apply_url)
    if not tenant or not external_id or len(external_id) > 255:
        return None
    return f"{normalized_source}:{tenant.casefold()}", external_id


def trustworthy_posting_date_source(source: str | None) -> bool:
    """Only direct ATS responses can authorize the URL/date fallback rule."""
    return (source or "").strip().casefold() in _DIRECT_ATS_SOURCES


def _identity_from_url(value: str) -> tuple[str, str] | None:
    canonical = canonicalize_apply_url(value)
    parts = urlsplit(canonical)
    host = (parts.hostname or "").casefold().removeprefix("www.")
    path = [unquote(segment) for segment in parts.path.split("/") if segment]

    if _on_domain(host, "greenhouse.io") and len(path) >= 3 and path[1] in {"jobs", "job"}:
        return f"greenhouse:{path[0].casefold()}", path[2][:255]
    if _on_domain(host, "lever.co") and len(path) >= 2:
        return f"lever:{path[0].casefold()}", path[1][:255]
    if _on_domain(host, "ashbyhq.com") and len(path) >= 2:
        return f"ashby:{path[0].casefold()}", path[1][:255]

    if _on_domain(host, "myworkdayjobs.com"):
        for index, segment in enumerate(path[:-1]):
            if segment.casefold() in {"job", "jobs"}:
                external_id = path[index + 1]
                if external_id:
                    return f"workday:{host}", external_id[:255]
    if _on_domain(host, "amazon.jobs"):
        for index, segment in enumerate(path[:-1]):
            if segment.casefold() == "jobs" and path[index + 1]:
                return f"amazon:{host}", path[index + 1][:255]
    if _on_domain(host, "metacareers.com"):
        query_id = parse_qs(parts.query).get("job_id")
        if query_id and query_id[0]:
            return "meta:metacareers.com", query_id[0][:255]
        if len(path) >= 2 and path[-2].casefold() == "jobs":
            return "meta:metacareers.com", path[-1][:255]
    return None


def _tenant_from_url(value: str) -> str | None:
    parts = urlsplit(canonicalize_apply_url(value))
    host = (parts.hostname or "").casefold()
    path = [unquote(segment) for segment in parts.path.split("/") if segment]
    if _on_domain(host, "lever.co") and path:
        return path[0]
    if _on_domain(host, "greenhouse.io") and path:
        return path[0]
    return None


def _on_domain(host: str, domain: str) -> bool:
    """Match the provider domain or one of its real subdomains."""
    return host == domain or host.endswith(f".{domain}")
