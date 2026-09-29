"""Deterministic authority rules for observations of a posting's location."""

from __future__ import annotations

import unicodedata

from app.schemas.job import NormalizedJob
from app.services.hasher import generate_content_hash

# Higher values are allowed to correct lower-authority observations. Keep these
# labels aligned with RawJobPayload.source values emitted by the scrapers.
_SOURCE_TIERS = {
    "applyguy_internships": 1,
    "applyguy_new_grad": 1,
    "simplify_github": 2,
    "amazon_jobs": 3,
    "greenhouse": 3,
    "lever": 3,
    "meta_careers": 3,
    "workday": 3,
}


def is_meaningful_location(value: str | None) -> bool:
    """Whether a location conveys information beyond the ingestion fallback."""
    if value is None:
        return False
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    return bool(normalized) and normalized not in {"unspecified", "unknown", "n/a"}


def _source_name(source: str | None) -> str | None:
    normalized = " ".join(source.split()).casefold() if source else ""
    return normalized or None


def _source_wins(candidate: str | None, current: str | None) -> bool:
    """Compare provenance without assuming unrecognized sources are strong."""
    candidate_name = _source_name(candidate)
    current_name = _source_name(current)
    if candidate_name is None:
        # Callers that predate provenance retain their historical same-authority
        # update behavior while both sides remain unprovenanced.
        return current_name is None
    if current_name is None:
        return candidate_name in _SOURCE_TIERS
    if candidate_name == current_name:
        return True
    candidate_tier = _SOURCE_TIERS.get(candidate_name, 0)
    current_tier = _SOURCE_TIERS.get(current_name, 0)
    if candidate_tier != current_tier:
        return candidate_tier > current_tier
    if candidate_tier == 0:
        # Different unrecognized sources have no evidence-based ordering.
        return False
    # Fixed tie-break avoids ping-pong between equal-tier source families.
    return candidate_name < current_name


def reconcile_location(
    job: NormalizedJob,
    *,
    existing_location: str | None,
    existing_source: str | None,
) -> NormalizedJob:
    """Choose the effective location and source before computing content state.

    A source can always correct its own prior observation. More authoritative
    sources can correct weaker observations. Equal-tier sources use a stable
    lexical tie-break, while distinct unknown sources cannot replace one another.
    """
    incoming_location = job.location
    incoming_source = _source_name(job.location_source)
    if not is_meaningful_location(incoming_location):
        effective_location = existing_location or incoming_location
        effective_source = existing_source
    elif not is_meaningful_location(existing_location):
        effective_location = incoming_location
        effective_source = incoming_source
    elif _source_wins(incoming_source, existing_source):
        effective_location = incoming_location
        effective_source = incoming_source
    else:
        effective_location = existing_location or incoming_location
        effective_source = existing_source

    content_hash = job.content_hash
    if effective_location != job.location:
        content_hash = generate_content_hash(
            job.base_hash,
            str(job.apply_url),
            effective_location,
            job.is_closed,
        )
    return job.model_copy(
        update={
            "location": effective_location,
            "location_source": effective_source,
            "content_hash": content_hash,
        }
    )


__all__ = ["is_meaningful_location", "reconcile_location"]
