"""Atomic Redis-backed classification of ingested job states."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from typing import Any, ClassVar, Self

from redis.asyncio import Redis

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_TTL_SECONDS = 90 * 24 * 60 * 60
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DeduplicationState(StrEnum):
    """Possible outcomes when comparing an incoming job with cached state."""

    NEW_ROLE = "NEW_ROLE"
    ROLE_UPDATED = "ROLE_UPDATED"
    ROLE_CLOSED = "ROLE_CLOSED"
    NO_OP = "NO_OP"


class JobDeduplicator:
    """Classify and cache job content hashes with an atomic Redis operation.

    A client passed to the constructor remains owned by the caller. Clients made
    by :meth:`from_url` are owned by this service and closed by :meth:`aclose`.
    """

    _COMPARE_AND_UPDATE: ClassVar[str] = """
local previous = redis.call("GET", KEYS[1])
local content_hash = ARGV[1]
local is_closed = ARGV[2]
local ttl_seconds = tonumber(ARGV[3])

if not previous then
    redis.call("SET", KEYS[1], content_hash, "EX", ttl_seconds)
    return 1
end

if previous == content_hash then
    redis.call("EXPIRE", KEYS[1], ttl_seconds)
    return 4
end

redis.call("SET", KEYS[1], content_hash, "EX", ttl_seconds)
if is_closed == "1" then
    return 3
end
return 2
"""
    _RESULTS: ClassVar[dict[int, DeduplicationState]] = {
        1: DeduplicationState.NEW_ROLE,
        2: DeduplicationState.ROLE_UPDATED,
        3: DeduplicationState.ROLE_CLOSED,
        4: DeduplicationState.NO_OP,
    }

    def __init__(
        self,
        client: Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        key_namespace: str = "jobping:dedupe",
        owns_client: bool = False,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        normalized_namespace = key_namespace.strip().strip(":")
        if not normalized_namespace or any(char.isspace() for char in normalized_namespace):
            raise ValueError("key_namespace must be non-empty and contain no whitespace")

        self._client = client
        self._ttl_seconds = ttl_seconds
        self._key_namespace = normalized_namespace
        self._owns_client = owns_client
        self._closed = False

    @classmethod
    def from_url(
        cls,
        url: str | None = None,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        key_namespace: str = "jobping:dedupe",
    ) -> Self:
        """Create a deduplicator that owns a connection-pooling Redis client."""
        redis_url = url if url is not None else os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
        if not redis_url.strip():
            raise ValueError("Redis URL must be non-empty")
        client = Redis.from_url(redis_url, decode_responses=True)
        return cls(
            client,
            ttl_seconds=ttl_seconds,
            key_namespace=key_namespace,
            owns_client=True,
        )

    async def classify_and_update(
        self,
        *,
        base_hash: str,
        content_hash: str,
        is_closed: bool,
    ) -> DeduplicationState:
        """Atomically classify an incoming state and store its content hash."""
        self._validate_hash(base_hash, name="base_hash")
        self._validate_hash(content_hash, name="content_hash")
        if not isinstance(is_closed, bool):
            raise TypeError("is_closed must be a boolean")
        if self._closed:
            raise RuntimeError("JobDeduplicator is closed")

        result: Any = await self._client.eval(
            self._COMPARE_AND_UPDATE,
            1,
            self._cache_key(base_hash),
            content_hash,
            "1" if is_closed else "0",
            self._ttl_seconds,
        )
        if isinstance(result, bool) or not isinstance(result, int) or result not in self._RESULTS:
            raise RuntimeError(f"Redis returned an unknown deduplication result: {result!r}")
        return self._RESULTS[result]

    async def aclose(self) -> None:
        """Close only a Redis client created and owned by this service."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    def _cache_key(self, base_hash: str) -> str:
        return f"{self._key_namespace}:{base_hash}"

    @staticmethod
    def _validate_hash(value: str, *, name: str) -> None:
        if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{name} must be a lowercase 64-character SHA-256 hex digest")
