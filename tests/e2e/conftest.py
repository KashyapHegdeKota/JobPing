"""Controlled Chromium pages with all browser requests intercepted."""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from app.scrapers.browser import BrowserManager
from playwright.async_api import Page, Route


@pytest_asyncio.fixture
async def application_page() -> AsyncIterator[Page]:
    if os.getenv("RUN_BROWSER_E2E") != "1":
        pytest.skip("set RUN_BROWSER_E2E=1 to exercise the controlled application fixture")
    html = (
        Path(__file__).parents[1] / "fixtures/application_agent/controlled_application.html"
    ).read_text(encoding="utf-8")
    async with BrowserManager(block_resources=frozenset()) as manager:
        page = await manager.new_page()
        try:

            async def serve(route: Route) -> None:
                if route.request.url == "https://boards.greenhouse.io/fixture/jobs/123":
                    await route.fulfill(status=200, content_type="text/html", body=html)
                else:
                    await route.abort()

            await page.route("**/*", serve)
            await page.goto("https://boards.greenhouse.io/fixture/jobs/123")
            yield page
        finally:
            await page.close()
