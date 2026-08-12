"""Fake-Playwright tests for asynchronous network payload capture."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from app.scrapers.network_interceptor import NetworkInterceptor
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Response


class FakePage:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}
        self.removed: list[tuple[str, object]] = []

    def on(self, event: str, callback: object) -> None:
        self.listeners[event] = callback

    def remove_listener(self, event: str, callback: object) -> None:
        self.removed.append((event, callback))
        self.listeners.pop(event, None)

    def emit_response(self, response: Response) -> None:
        callback = self.listeners["response"]
        assert callable(callback)
        callback(response)


def response(
    payload: object = None,
    *,
    url: str = "https://jobs.test/api/jobs?token=secret&team=eng",
    status: int = 200,
    method: str = "GET",
    resource_type: str = "xhr",
    content_type: str = "application/json",
    error: Exception | None = None,
) -> Response:
    request = Mock(spec=Request)
    request.method = method
    request.resource_type = resource_type
    item = Mock(spec=Response)
    item.url = url
    item.status = status
    item.headers = {"content-type": content_type}
    item.request = request
    item.json = AsyncMock(side_effect=error, return_value=payload)
    return cast(Response, item)


async def settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_attach_capture_metadata_redaction_snapshot_and_drain() -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    item = response({"jobs": [1]}, method="POST")
    page.emit_response(item)
    captured = await interceptor.wait_for_payload(timeout=1)

    assert captured.url == "https://jobs.test/api/jobs?token=%5BREDACTED%5D&team=eng"
    assert captured.status == 200
    assert captured.method == "POST"
    assert captured.payload == {"jobs": [1]}
    assert captured.content_type == "application/json"
    assert captured.captured_at.tzinfo is not None
    assert captured.estimated_bytes > 0
    assert interceptor.snapshot() == (captured,)
    assert interceptor.drain() == (captured,)
    assert interceptor.snapshot() == ()
    await interceptor.aclose()
    assert page.removed and "response" not in page.listeners


@pytest.mark.parametrize(
    "item",
    [
        response({}, status=404),
        response({}, url="https://jobs.test/assets/data", resource_type="document"),
        response({}, content_type="text/html", url="https://jobs.test/list", resource_type="xhr"),
    ],
)
async def test_irrelevant_responses_are_filtered(item: Response) -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    page.emit_response(item)
    await settle()
    assert interceptor.snapshot() == ()
    item.json.assert_not_awaited()  # type: ignore[attr-defined]
    await interceptor.aclose()


async def test_graphql_url_is_captured_with_jsonish_content_type() -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    page.emit_response(
        response(
            {"data": {"jobs": []}},
            url="https://jobs.test/graphql",
            resource_type="document",
            content_type="application/graphql-response+json",
        )
    )
    assert (await interceptor.wait_for_payload(timeout=1)).payload == {"data": {"jobs": []}}
    await interceptor.aclose()


@pytest.mark.parametrize(
    "error",
    [ValueError("invalid json"), PlaywrightError("body unavailable")],
)
async def test_invalid_json_and_navigation_teardown_are_tolerated(error: Exception) -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    page.emit_response(response(error=error))
    await settle()
    assert interceptor.snapshot() == ()
    await interceptor.aclose()


async def test_repeated_responses_are_deduplicated() -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    page.emit_response(response({"jobs": [1]}))
    page.emit_response(response({"jobs": [1]}))
    await settle()
    assert len(interceptor.snapshot()) == 1
    await interceptor.aclose()


async def test_count_and_byte_bounds_evict_oldest_and_allow_recapture() -> None:
    page = FakePage()
    interceptor = NetworkInterceptor(max_payloads=2, max_estimated_bytes=20).attach(
        cast(Page, page)
    )
    page.emit_response(response({"id": 1}))
    page.emit_response(response({"id": 2}))
    page.emit_response(response({"id": 3}))
    await settle()
    assert [item.payload for item in interceptor.snapshot()] == [{"id": 2}, {"id": 3}]
    page.emit_response(response({"id": 1}))
    await settle()
    assert [item.payload for item in interceptor.snapshot()] == [{"id": 3}, {"id": 1}]
    await interceptor.aclose()


async def test_wait_for_payloads_predicate_and_timeout() -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    waiter = asyncio.create_task(
        interceptor.wait_for_payloads(
            count=2,
            timeout=1,
            predicate=lambda item: isinstance(item.payload, dict)
            and item.payload.get("kind") == "job",
        )
    )
    page.emit_response(response({"kind": "other"}))
    page.emit_response(response({"kind": "job", "id": 1}))
    page.emit_response(response({"kind": "job", "id": 2}))
    assert len(await waiter) == 2
    with pytest.raises(TimeoutError):
        await interceptor.wait_for_payload(timeout=0.01, predicate=lambda _: False)
    await interceptor.aclose()


async def test_close_detaches_cancels_and_awaits_callback_tasks() -> None:
    page = FakePage()
    interceptor = NetworkInterceptor().attach(cast(Page, page))
    started = asyncio.Event()

    async def blocked_json() -> object:
        started.set()
        await asyncio.Event().wait()
        return {}

    item = response({})
    item.json.side_effect = blocked_json  # type: ignore[attr-defined]
    page.emit_response(item)
    await started.wait()
    await interceptor.aclose()
    assert page.removed
    assert not interceptor._tasks
    await interceptor.aclose()


async def test_body_task_preserves_external_cancellation() -> None:
    item = response(error=asyncio.CancelledError())
    interceptor = NetworkInterceptor()
    task = asyncio.create_task(interceptor._capture(item))
    with pytest.raises(asyncio.CancelledError):
        await task


def test_attach_and_configuration_guards() -> None:
    page = cast(Page, FakePage())
    interceptor = NetworkInterceptor().attach(page)
    with pytest.raises(RuntimeError, match="already attached"):
        interceptor.attach(page)
    with pytest.raises(ValueError):
        NetworkInterceptor(max_payloads=0)
    with pytest.raises(ValueError):
        NetworkInterceptor(max_estimated_bytes=0)
