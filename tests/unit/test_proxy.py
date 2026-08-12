"""Tests for proxy configuration, rotation, and health."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from app.scrapers.proxy import NoProxyAvailableError, ProxyEndpoint, ProxyError, ProxyManager


def test_parse_redacts_and_builds_playwright_config() -> None:
    endpoint = ProxyEndpoint.parse("http://user:p%40ss@proxy.example:8080")
    assert str(endpoint) == "http://***@proxy.example:8080"
    assert "user" not in repr(str(endpoint))
    assert endpoint.playwright() == {
        "server": "http://proxy.example:8080",
        "username": "user",
        "password": "p@ss",
    }


@pytest.mark.parametrize("value", ["", "ftp://host:2", "http://host", "http://host:99999"])
def test_parse_rejects_invalid_urls(value: str) -> None:
    with pytest.raises(ValueError):
        ProxyEndpoint.parse(value)


@pytest.mark.asyncio
async def test_rotation_is_round_robin_and_concurrency_safe() -> None:
    manager = ProxyManager(["http://one:1", "http://two:2", "http://three:3"])
    acquired = await asyncio.gather(*(manager.acquire() for _ in range(6)))
    assert [item.host for item in acquired] == ["one", "two", "three", "one", "two", "three"]


@pytest.mark.asyncio
async def test_cooldown_dead_and_recovery() -> None:
    now = [100.0]
    manager = ProxyManager(
        ["http://one:1"], clock=lambda: now[0], base_cooldown=10, max_cooldown=20, max_failures=2
    )
    endpoint = await manager.acquire()
    await manager.report_failure(endpoint, 429)
    assert manager.health(endpoint).cooldown_until == 110
    with pytest.raises(NoProxyAvailableError):
        await manager.acquire()
    now[0] = 110
    endpoint = await manager.acquire()
    await manager.report_failure(endpoint, 403)
    assert manager.health(endpoint).dead
    now[0] = 130
    assert await manager.acquire() == endpoint
    assert not manager.health(endpoint).dead
    await manager.report_success(endpoint)
    assert manager.health(endpoint).successes == 1


@pytest.mark.asyncio
async def test_non_blocking_failure_tracks_without_cooldown() -> None:
    manager = ProxyManager(["https://one:443"])
    endpoint = await manager.acquire()
    await manager.report_failure(endpoint, 500)
    assert manager.health(endpoint).failures == 1
    assert await manager.acquire() == endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/plain", "http://one:1\nhttp://two:2"),
        ("application/json", '{"proxies":["http://one:1"]}'),
    ],
)
async def test_provider_contract(content_type: str, body: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=body, headers={"content-type": content_type}, request=request
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        manager = await ProxyManager.from_provider("https://provider.example/list", client=client)
    assert (await manager.acquire()).host == "one"


@pytest.mark.asyncio
async def test_provider_failure_hides_response_details() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProxyError, match="provider request failed") as error:
            await ProxyManager.from_provider(
                "https://user:secret@provider.example/list", client=client
            )
    assert "secret" not in str(error.value)


def test_environment_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_LIST", "http://one:1,\nhttps://two:2")
    manager = ProxyManager()
    assert len(manager._endpoints) == 2
