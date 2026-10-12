"""Invalid rollout settings fail without echoing configuration or secrets."""

import pytest
from app.request_limits.settings import LimitSettings


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_modes(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_LIMIT_MODE", mode)
    monkeypatch.setenv("REQUEST_LIMIT_SECRET", "private-secret-" * 4)
    settings = LimitSettings.from_env()
    assert settings.mode == mode
    assert "private-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REQUEST_LIMIT_MODE", "unknown"),
        ("REQUEST_LIMIT_SECRET", "short"),
        ("REQUEST_LIMIT_TIMEOUT", "nan"),
        ("REQUEST_LIMIT_LEASE_SECONDS", "0"),
        ("REQUEST_LIMIT_STREAMS_TOTAL", "-1"),
        ("REQUEST_LIMIT_CACHE_SECONDS", "300"),
        ("REQUEST_LIMIT_POLICIES", '{"unknown":{"rate":1,"burst":1}}'),
        ("REQUEST_LIMIT_POLICIES", '{"public":{"rate":0,"burst":1}}'),
        ("REQUEST_LIMIT_POLICIES", '{"public":{"rate":1,"burst":true}}'),
        ("REQUEST_LIMIT_POLICIES", '"secret-test-value"'),
    ],
)
def test_invalid_settings_are_redacted(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REQUEST_LIMIT_MODE", "enforce")
    monkeypatch.setenv("REQUEST_LIMIT_SECRET", "s" * 32)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="check server settings") as error:
        LimitSettings.from_env()
    assert "secret-test-value" not in str(error.value)


def test_policy_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REQUEST_LIMIT_POLICIES", '{"public":{"rate":0.5,"burst":7}}')
    settings = LimitSettings.from_env()
    assert settings.policies["public"].rate == 0.5
    assert settings.policies["public"].burst == 7
