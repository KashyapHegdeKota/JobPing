"""Opt-in end-to-end coverage for the Playwright browser runtime."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from app.scrapers.browser import BrowserManager
from app.scrapers.network_interceptor import NetworkInterceptor
from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route

E2E_ENV = "RUN_BROWSER_E2E"


@pytest_asyncio.fixture
async def real_page() -> AsyncIterator[Page]:
    """Launch local Chromium or skip with an actionable, explicit reason."""
    if os.environ.get(E2E_ENV) != "1":
        pytest.skip(f"browser E2E disabled; set {E2E_ENV}=1 to enable")

    manager = BrowserManager(block_resources=frozenset())
    try:
        from playwright.async_api import async_playwright

        probe = await async_playwright().start()
        executable = Path(probe.chromium.executable_path)
        await probe.stop()
        if not executable.is_file():
            pytest.skip(
                "Playwright Chromium binary is absent; run "
                "`poetry run playwright install chromium` explicitly"
            )
        await manager.start()
        yield await manager.new_page()
    finally:
        await manager.aclose()


@pytest.mark.browser_e2e
async def test_real_chromium_stealth_and_local_json_interception(real_page: Page) -> None:
    """Verify launch, stealth, and JSON capture without internet access."""
    page = real_page

    async def fulfill_api(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            headers={"access-control-allow-origin": "*"},
            body='{"jobs":[{"id":"local-1"}]}',
        )

    await page.route("https://jobping.local/api/jobs*", fulfill_api)
    interceptor = NetworkInterceptor(url_patterns=(r"/api/jobs",)).attach(page)
    await page.set_content("<main id='ready'>JobPing E2E</main>")

    assert await page.locator("#ready").text_content() == "JobPing E2E"
    assert await page.evaluate("navigator.webdriver") is False
    assert await page.evaluate("navigator.languages.length > 0") is True

    with pytest.raises(TimeoutError):
        await interceptor.wait_for_payload(timeout=0.01)

    await page.evaluate("fetch('https://jobping.local/api/jobs?token=private').then(r => r.json())")
    captured = await interceptor.wait_for_payload(timeout=2)
    assert captured.payload == {"jobs": [{"id": "local-1"}]}
    assert "private" not in captured.url
    assert "%5BREDACTED%5D" in captured.url
    await interceptor.aclose()


async def test_proxy_configuration_is_attached_without_contacting_proxy() -> None:
    """Verify launch wiring deterministically; a fake proxy is never contacted."""
    context = Mock(spec=BrowserContext)
    context.close = AsyncMock()
    browser = Mock(spec=Browser)
    browser.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    chromium = Mock()
    chromium.launch = AsyncMock(return_value=browser)
    playwright = Mock(spec=Playwright)
    playwright.chromium = chromium
    playwright.stop = AsyncMock()
    manager = BrowserManager(
        playwright=cast(Playwright, playwright),
        proxy={
            "server": "http://127.0.0.1:9",
            "username": "proxy-user",
            "password": "proxy-password",
        },
    )

    await manager.start()

    chromium.launch.assert_awaited_once_with(
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
        proxy={
            "server": "http://127.0.0.1:9",
            "username": "proxy-user",
            "password": "proxy-password",
        },
    )
    await manager.aclose()
