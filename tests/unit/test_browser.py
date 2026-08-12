"""Mock-only tests for the managed browser runtime."""

from __future__ import annotations

import random
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from app.scrapers.browser import BrowserManager, BrowserProfile
from playwright.async_api import Browser, BrowserContext, Playwright, Request, Route
from playwright_stealth import Stealth


def runtime() -> tuple[Playwright, Browser, BrowserContext]:
    context = Mock(spec=BrowserContext)
    context.route = AsyncMock()
    context.new_page = AsyncMock(return_value=Mock())
    context.close = AsyncMock()
    browser = Mock(spec=Browser)
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    chromium = Mock()
    chromium.launch = AsyncMock(return_value=browser)
    playwright = Mock(spec=Playwright)
    playwright.chromium = chromium
    playwright.stop = AsyncMock()
    return cast(Playwright, playwright), cast(Browser, browser), cast(BrowserContext, context)


async def test_launch_context_hardening_and_external_lifecycle() -> None:
    playwright, browser, context = runtime()
    stealth = Mock(spec=Stealth)
    stealth.apply_stealth_async = AsyncMock()
    manager = BrowserManager(
        playwright=playwright,
        headless=False,
        proxy={"server": "http://proxy.test:8080"},
        navigation_timeout_ms=12_000,
        user_agents=("Test Agent",),
        viewports=((1440, 900),),
        stealth=cast(Stealth, stealth),
    )

    page = await manager.new_page()

    playwright.chromium.launch.assert_awaited_once_with(  # type: ignore[attr-defined]
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        proxy={"server": "http://proxy.test:8080"},
    )
    browser.new_context.assert_awaited_once_with(  # type: ignore[attr-defined]
        user_agent="Test Agent",
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
        },
    )
    context.set_default_navigation_timeout.assert_called_once_with(12_000)  # type: ignore[attr-defined]
    stealth.apply_stealth_async.assert_awaited_once_with(context)
    context.route.assert_awaited_once()  # type: ignore[attr-defined]
    context.new_page.assert_awaited_once()  # type: ignore[attr-defined]
    assert page is context.new_page.return_value  # type: ignore[attr-defined]

    await manager.aclose()
    context.close.assert_awaited_once()  # type: ignore[attr-defined]
    browser.close.assert_awaited_once()  # type: ignore[attr-defined]
    playwright.stop.assert_not_awaited()  # type: ignore[attr-defined]


def test_profile_selection_is_deterministic_with_injected_rng() -> None:
    manager = BrowserManager(
        playwright=runtime()[0],
        user_agents=("Agent A", "Agent B"),
        viewports=((1000, 700), (1200, 800)),
        rng=random.Random(7),
    )
    assert manager.choose_profile() == BrowserProfile("Agent B", 1000, 700)


@pytest.mark.parametrize(
    ("resource_type", "aborted"),
    [("image", True), ("font", True), ("media", True), ("stylesheet", False), ("xhr", False)],
)
async def test_resource_routing(resource_type: str, aborted: bool) -> None:
    manager = BrowserManager(playwright=runtime()[0])
    route = Mock(spec=Route)
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = Mock(spec=Request)
    request.resource_type = resource_type

    await manager._route_resource(cast(Route, route), cast(Request, request))

    assert route.abort.await_count == int(aborted)
    assert route.continue_.await_count == int(not aborted)


async def test_stylesheet_blocking_is_explicitly_opt_in() -> None:
    manager = BrowserManager(playwright=runtime()[0], block_resources=frozenset({"stylesheet"}))
    route = Mock(spec=Route)
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    request = Mock(spec=Request)
    request.resource_type = "stylesheet"
    await manager._route_resource(cast(Route, route), cast(Request, request))
    route.abort.assert_awaited_once()


async def test_cleanup_continues_and_groups_errors() -> None:
    playwright, browser, context = runtime()
    context.close.side_effect = RuntimeError("context failed")  # type: ignore[attr-defined]
    browser.close.side_effect = RuntimeError("browser failed")  # type: ignore[attr-defined]
    manager = BrowserManager(playwright=playwright)
    await manager.new_context()

    with pytest.raises(ExceptionGroup) as captured:
        await manager.aclose()
    assert len(captured.value.exceptions) == 2
    playwright.stop.assert_not_awaited()  # type: ignore[attr-defined]


async def test_close_is_idempotent_and_closed_manager_cannot_restart() -> None:
    playwright, browser, _ = runtime()
    manager = BrowserManager(playwright=playwright)
    await manager.start()
    await manager.aclose()
    await manager.aclose()
    browser.close.assert_awaited_once()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="closed"):
        await manager.start()


async def test_owned_playwright_runtime_is_started_and_stopped() -> None:
    playwright, _, _ = runtime()
    starter = Mock()
    starter.start = AsyncMock(return_value=playwright)
    with patch("app.scrapers.browser.async_playwright", return_value=starter):
        manager = BrowserManager()
        await manager.start()
        await manager.aclose()

    starter.start.assert_awaited_once()
    playwright.stop.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "changes",
    [
        {"navigation_timeout_ms": 0},
        {"user_agents": ()},
        {"viewports": ((0, 800),)},
        {"block_resources": frozenset({"script"})},
    ],
)
def test_invalid_configuration_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BrowserManager(playwright=runtime()[0], **changes)  # type: ignore[arg-type]
