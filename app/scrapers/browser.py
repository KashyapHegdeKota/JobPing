"""Managed Playwright runtime for hardened dynamic-portal scraping."""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    ProxySettings,
    Request,
    Route,
    async_playwright,
)
from playwright_stealth import Stealth

DEFAULT_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
DEFAULT_VIEWPORTS = ((1366, 768), (1440, 900), (1536, 864), (1920, 1080))


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    """Browser identity values selected for one isolated context."""

    user_agent: str
    viewport_width: int
    viewport_height: int


class BrowserManager:
    """Own or reuse Playwright while managing Chromium contexts and pages.

    Stealth scripts provide browser hardening, not a guarantee against bot
    detection. Images, fonts, and media are blocked by default. Stylesheets
    remain enabled because many SPAs depend on CSS-driven visibility.
    """

    def __init__(
        self,
        *,
        playwright: Playwright | None = None,
        headless: bool = True,
        proxy: ProxySettings | None = None,
        navigation_timeout_ms: float = 30_000,
        block_resources: frozenset[str] = frozenset({"image", "font", "media"}),
        user_agents: tuple[str, ...] = DEFAULT_USER_AGENTS,
        viewports: tuple[tuple[int, int], ...] = DEFAULT_VIEWPORTS,
        locale: str = "en-US",
        timezone_id: str = "America/New_York",
        extra_http_headers: Mapping[str, str] | None = None,
        rng: random.Random | None = None,
        stealth: Stealth | None = None,
    ) -> None:
        if navigation_timeout_ms <= 0:
            raise ValueError("navigation_timeout_ms must be positive")
        if not user_agents or not all(item.strip() for item in user_agents):
            raise ValueError("user_agents must contain at least one non-empty value")
        if not viewports or any(width <= 0 or height <= 0 for width, height in viewports):
            raise ValueError("viewports must contain positive dimensions")
        supported = {"image", "font", "media", "stylesheet"}
        if unsupported := block_resources - supported:
            raise ValueError(f"unsupported blocked resource types: {sorted(unsupported)}")

        self._playwright = playwright
        self._owns_playwright = playwright is None
        self._headless = headless
        self._proxy = proxy
        self._navigation_timeout_ms = navigation_timeout_ms
        self._block_resources = block_resources
        self._user_agents = user_agents
        self._viewports = viewports
        self._locale = locale
        self._timezone_id = timezone_id
        self._headers = dict(
            extra_http_headers
            or {
                "Accept-Language": "en-US,en;q=0.9",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self._rng = rng or random.Random()
        self._stealth = stealth or Stealth()
        self._browser: Browser | None = None
        self._contexts: list[BrowserContext] = []
        self._closed = False

    async def start(self) -> Self:
        """Start Playwright when owned and launch one Chromium browser."""
        self._ensure_open()
        if self._browser is not None:
            return self
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        launch_options: dict[str, object] = {
            "headless": self._headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self._proxy is not None:
            launch_options["proxy"] = self._proxy
        self._browser = await self._playwright.chromium.launch(**launch_options)
        return self

    def choose_profile(self) -> BrowserProfile:
        """Select a user agent and viewport through the injectable RNG."""
        user_agent = self._rng.choice(self._user_agents)
        width, height = self._rng.choice(self._viewports)
        return BrowserProfile(user_agent, width, height)

    async def new_context(self) -> BrowserContext:
        """Create, harden, configure, and track an isolated browser context."""
        await self.start()
        assert self._browser is not None
        profile = self.choose_profile()
        context = await self._browser.new_context(
            user_agent=profile.user_agent,
            viewport={"width": profile.viewport_width, "height": profile.viewport_height},
            locale=self._locale,
            timezone_id=self._timezone_id,
            extra_http_headers=self._headers,
        )
        context.set_default_navigation_timeout(self._navigation_timeout_ms)
        await self._stealth.apply_stealth_async(context)
        if self._block_resources:
            await context.route("**/*", self._route_resource)
        self._contexts.append(context)
        return context

    async def new_page(self) -> Page:
        """Create a page in a new hardened context."""
        return await (await self.new_context()).new_page()

    async def _route_resource(self, route: Route, request: Request) -> None:
        if request.resource_type in self._block_resources:
            await route.abort()
        else:
            await route.continue_()

    async def aclose(self) -> None:
        """Close contexts/browser and stop only an owned Playwright runtime."""
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for context in reversed(self._contexts):
            try:
                await context.close()
            except Exception as exc:
                errors.append(exc)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:
                errors.append(exc)
        if self._owns_playwright and self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("browser cleanup failed", errors)

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("BrowserManager is closed")


__all__ = ["BrowserManager", "BrowserProfile"]
