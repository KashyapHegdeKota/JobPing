"""Read-only Chromium rendering with controlled public HTTP and blocked browser writes."""

# The controlled HTML deliberately includes executable write instrumentation.
# ruff: noqa: E501

import os

import httpx
import pytest
from app.evidence.fetch import Fetcher
from app.evidence.renderer import RenderRequest, app, health, lifespan, render

pytestmark = pytest.mark.browser_e2e


async def test_renderer_reads_labels_without_filling_or_submission() -> None:
    if os.environ.get("RUN_BROWSER_E2E") != "1":
        pytest.skip("set RUN_BROWSER_E2E=1 for controlled renderer acceptance")
    requests: list[httpx.Request] = []
    html = """<html><body><main id="content"><h1>Fixture role</h1>
    <p>This position does not provide visa sponsorship now or in the future.</p>
    <p>Base salary USD 80000-120000 per year.</p>
    <label>Name<input value="candidate-secret" oninput="document.querySelector('#writes').textContent='filled'"></label>
    <textarea>answer-secret</textarea><p hidden>hidden-secret</p>
    <p id="writes">No fields changed</p>
    <aside>We accept OPT. Recommended unrelated job.</aside>
    <form onsubmit="document.querySelector('#writes').textContent='submitted'; return false"><button>Submit application</button></form>
    <script>fetch('/attempted-write', {method:'POST', body:'secret'}).catch(()=>{});</script>
    </main></body></html>"""

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    async with lifespan(app):
        assert await health() == {"status": "ok"}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            app.state.fetcher = Fetcher(client)
            result = await render(RenderRequest(url="https://fixture.example/role"))
    assert "No fields changed" in result["text"]
    assert "sponsorship now or in the future" in result["text"]
    assert "secret" not in result["text"]
    assert "We accept OPT" not in result["text"]
    assert all(request.method == "GET" for request in requests)
    assert not any(request.url.path == "/attempted-write" for request in requests)
