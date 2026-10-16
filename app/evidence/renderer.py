"""Secret-free read-only renderer. Every browser request is fulfilled by pinned public HTTP."""

# Executable DOM probe is kept together for inspection.
# ruff: noqa: E501

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from playwright.async_api import Error as BrowserError
from playwright.async_api import Route, async_playwright
from pydantic import BaseModel, ConfigDict, Field

from app.evidence.collect import ROLE_SELECTOR, Collector, PublicHTML
from app.evidence.fetch import Fetcher, FetchError, PublicTransport


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(transport=PublicTransport(), trust_env=False) as client:
        app.state.fetcher = Fetcher(client)
        app.state.lock = asyncio.Semaphore(1)
        async with async_playwright() as playwright:
            app.state.browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--proxy-server=http://127.0.0.1:9",
                    "--proxy-bypass-list=<-loopback>",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
                ],
            )
            try:
                yield
            finally:
                await app.state.browser.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/render")
async def render(body: RenderRequest) -> dict:
    async with app.state.lock:
        try:
            async with asyncio.timeout(40):
                context = await app.state.browser.new_context(
                    service_workers="block", locale="en-US"
                )
                count = 0

                async def public_request(route: Route) -> None:
                    nonlocal count
                    count += 1
                    if (
                        count > 60
                        or route.request.method != "GET"
                        or route.request.resource_type in {"image", "font", "media"}
                    ):
                        await route.abort()
                        return
                    try:
                        page = await app.state.fetcher.get(route.request.url)
                        await route.fulfill(status=200, body=page.body, headers=page.headers)
                    except FetchError:
                        await route.abort()

                try:
                    await context.route("**/*", public_request)
                    await context.route_web_socket("**/*", lambda socket: socket.close())
                    page = await context.new_page()
                    await page.goto(body.url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_load_state("networkidle", timeout=5000)
                    text = await page.evaluate(
                        """selector => {
                        const output = [];
                        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                        while (walker.nextNode()) {
                            const node = walker.currentNode;
                            const element = node.parentElement;
                            if (!element || element.closest('script,style,noscript,input,textarea,select,[hidden],[aria-hidden="true"]')) continue;
                            if (!element.closest(selector)) continue;
                            if (element.closest('nav,footer,aside,[class*="related"],[class*="recommended"],[class*="similar-jobs"],[id*="related"],[id*="recommended"]')) continue;
                            const style = getComputedStyle(element);
                            if (style.display === 'none' || style.visibility === 'hidden' || !element.getClientRects().length) continue;
                            const value = node.textContent.replace(/\\s+/g, ' ').trim();
                            if (value) output.push((element.closest('label,legend') ? 'Question: ' : '') + value);
                        }
                        return output.join('\\n');
                    }""",
                        ROLE_SELECTOR,
                    )
                    if len(text) > 100_000 or Collector.barrier(text):
                        raise FetchError("verification_barrier")
                    parser = PublicHTML(page.url)
                    parser.feed(await page.content())
                    return {"url": page.url, "text": text, "structured": parser.structured[:20]}
                finally:
                    await context.close()
        except (FetchError, TimeoutError, BrowserError):
            raise HTTPException(422, "Public page could not be verified") from None
