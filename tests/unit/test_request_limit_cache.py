"""Cache cannot retain unbounded responses, cookies or stale reference data."""

from unittest.mock import patch

from app.request_limits.cache import PublicCache


def test_cache_expiry_and_capacity() -> None:
    cache = PublicCache(30, 2)
    with patch("app.request_limits.cache.time.monotonic", return_value=10):
        cache.put("one", [], b"1")
        cache.put("two", [], b"2")
        assert cache.get("one").body == b"1"
        cache.put("three", [], b"3")
        assert cache.get("two") is None
    with patch("app.request_limits.cache.time.monotonic", return_value=41):
        assert cache.get("one") is None


def test_cookie_large_payload_and_disabled_cache() -> None:
    cache = PublicCache(30)
    cache.put("cookie", [(b"Set-Cookie", b"private")], b"x")
    cache.put("large", [], b"x" * 262145)
    assert not cache.entries
    disabled = PublicCache(0)
    disabled.put("small", [], b"x")
    assert not disabled.entries
