"""Untrusted headers and IPv6 address churn do not produce fresh identities."""

import pytest
from app.request_limits.identity import client_address, opaque_key
from app.request_limits.policies import family


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.0.2.1", "192.0.2.1"),
        ("::ffff:192.0.2.1", "192.0.2.1"),
        ("2001:db8::1", "2001:db8::/64"),
        ("2001:db8::123", "2001:db8::/64"),
        ("invalid", "unknown"),
    ],
)
def test_normalization(address: str, expected: str) -> None:
    assert (
        client_address({"client": (address, 1), "headers": [(b"x-forwarded-for", b"1.2.3.4")]})
        == expected
    )


def test_opaque_namespaced_keys() -> None:
    key = opaque_key("secret", "uid:private_read", "account-sensitive")
    assert key.startswith("jobping:ratelimit:v1:uid:private_read:")
    assert "account-sensitive" not in key
    assert key != opaque_key("other", "uid:private_read", "account-sensitive")
    assert key != opaque_key("secret", "ip:private_read", "account-sensitive")


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/api/v1/jobs", "GET", "public"),
        ("/api/v1/companies", "GET", "public"),
        ("/api/v1/me/notification-settings", "PUT", "private_write"),
        ("/api/v1/analytics/events", "POST", "analytics"),
        ("/api/v1/analytics/me", "GET", "private_read"),
        ("/api/v1/sse/feed", "GET", "connect"),
        ("/api/v1/ws/live", "GET", "connect"),
        ("/api/v1/notifications/webhooks/random", "POST", "webhook"),
        ("/api/v1/notifications/unsubscribe/random", "GET", "unsubscribe"),
        ("/api/v1/jobs", "OPTIONS", "preflight"),
    ],
)
def test_policy_groups(path: str, method: str, expected: str) -> None:
    assert family(path, method) == expected
