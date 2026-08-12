"""Unit tests for atomic Redis-backed job state deduplication."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from app.services.deduplicator import DeduplicationState, JobDeduplicator
from redis.asyncio import Redis

BASE_HASH = "a" * 64
FIRST_CONTENT_HASH = "b" * 64
SECOND_CONTENT_HASH = "c" * 64


class AtomicRedisFake:
    """Minimal atomic model of the production Lua script with controllable time."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}
        self.now = 0.0
        self._lock = asyncio.Lock()

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        assert 'redis.call("GET"' in script
        assert numkeys == 1
        key, content_hash, is_closed, ttl = cast(tuple[str, str, str, int], args)
        async with self._lock:
            self._expire(key)
            previous = self.values.get(key)
            self.values[key] = content_hash
            self.expires_at[key] = self.now + ttl
            if previous is None:
                return 1
            if previous == content_hash:
                return 4
            return 3 if is_closed == "1" else 2

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for key in list(self.values):
            self._expire(key)

    def ttl(self, key: str) -> float | None:
        self._expire(key)
        expiry = self.expires_at.get(key)
        return None if expiry is None else expiry - self.now

    def _expire(self, key: str) -> None:
        if self.expires_at.get(key, float("inf")) <= self.now:
            self.values.pop(key, None)
            self.expires_at.pop(key, None)


def make_deduplicator(
    fake: AtomicRedisFake,
    *,
    ttl_seconds: int = 90 * 24 * 60 * 60,
    key_namespace: str = "jobping:dedupe",
) -> JobDeduplicator:
    """Adapt the intentionally narrow fake to the Redis client protocol."""
    return JobDeduplicator(cast(Redis, fake), ttl_seconds=ttl_seconds, key_namespace=key_namespace)


@pytest.mark.asyncio
async def test_new_role_is_stored_with_ttl_and_namespace() -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake, ttl_seconds=30, key_namespace="tenant:jobs")

    result = await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )

    key = f"tenant:jobs:{BASE_HASH}"
    assert result is DeduplicationState.NEW_ROLE
    assert fake.values[key] == FIRST_CONTENT_HASH
    assert fake.ttl(key) == 30


@pytest.mark.asyncio
async def test_changed_open_role_updates_stored_hash() -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake)
    await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )

    result = await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=SECOND_CONTENT_HASH, is_closed=False
    )

    assert result is DeduplicationState.ROLE_UPDATED
    assert fake.values[f"jobping:dedupe:{BASE_HASH}"] == SECOND_CONTENT_HASH


@pytest.mark.asyncio
async def test_changed_closed_role_updates_stored_hash() -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake)
    await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )

    result = await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=SECOND_CONTENT_HASH, is_closed=True
    )

    assert result is DeduplicationState.ROLE_CLOSED
    assert fake.values[f"jobping:dedupe:{BASE_HASH}"] == SECOND_CONTENT_HASH


@pytest.mark.asyncio
async def test_no_op_refreshes_ttl_without_changing_value() -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake, ttl_seconds=20)
    await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )
    fake.advance(12)

    result = await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )

    key = f"jobping:dedupe:{BASE_HASH}"
    assert result is DeduplicationState.NO_OP
    assert fake.values[key] == FIRST_CONTENT_HASH
    assert fake.ttl(key) == 20


@pytest.mark.asyncio
async def test_expired_entry_is_new_again() -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake, ttl_seconds=5)
    await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )
    fake.advance(5)

    result = await deduplicator.classify_and_update(
        base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
    )

    assert result is DeduplicationState.NEW_ROLE
    assert fake.values[f"jobping:dedupe:{BASE_HASH}"] == FIRST_CONTENT_HASH


@pytest.mark.asyncio
async def test_namespaces_isolate_the_same_base_hash() -> None:
    fake = AtomicRedisFake()
    first = make_deduplicator(fake, key_namespace="source:first")
    second = make_deduplicator(fake, key_namespace="source:second")

    results = await asyncio.gather(
        first.classify_and_update(
            base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
        ),
        second.classify_and_update(
            base_hash=BASE_HASH, content_hash=SECOND_CONTENT_HASH, is_closed=False
        ),
    )

    assert results == [DeduplicationState.NEW_ROLE, DeduplicationState.NEW_ROLE]
    assert fake.values[f"source:first:{BASE_HASH}"] == FIRST_CONTENT_HASH
    assert fake.values[f"source:second:{BASE_HASH}"] == SECOND_CONTENT_HASH


@pytest.mark.asyncio
async def test_concurrent_identical_events_have_one_new_role() -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake)

    results = await asyncio.gather(
        *(
            deduplicator.classify_and_update(
                base_hash=BASE_HASH, content_hash=FIRST_CONTENT_HASH, is_closed=False
            )
            for _ in range(20)
        )
    )

    assert results.count(DeduplicationState.NEW_ROLE) == 1
    assert results.count(DeduplicationState.NO_OP) == 19
    assert fake.values[f"jobping:dedupe:{BASE_HASH}"] == FIRST_CONTENT_HASH


@pytest.mark.parametrize("ttl", [0, -1, True, 1.5])
def test_invalid_ttl_is_rejected(ttl: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        make_deduplicator(AtomicRedisFake(), ttl_seconds=ttl)


@pytest.mark.parametrize("namespace", ["", "  ", ":", "job ping"])
def test_invalid_namespace_is_rejected(namespace: str) -> None:
    with pytest.raises(ValueError, match="key_namespace"):
        make_deduplicator(AtomicRedisFake(), key_namespace=namespace)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("base_hash", "A" * 64), ("content_hash", "x" * 64), ("is_closed", 1)],
)
async def test_invalid_classification_inputs_are_rejected(field: str, value: object) -> None:
    fake = AtomicRedisFake()
    deduplicator = make_deduplicator(fake)
    values: dict[str, object] = {
        "base_hash": BASE_HASH,
        "content_hash": FIRST_CONTENT_HASH,
        "is_closed": False,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        await deduplicator.classify_and_update(**values)
    assert fake.values == {}
