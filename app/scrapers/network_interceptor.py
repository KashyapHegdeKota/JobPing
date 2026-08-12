"""Bounded asynchronous capture of JSON API responses from Playwright pages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

_DEFAULT_URL_PATTERNS = (r"/api/", r"graphql")
_SECRET_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "authorization", "key", "secret", "token"}
)


@dataclass(frozen=True, slots=True)
class CapturedPayload:
    """One accepted browser response and its parsed JSON body."""

    url: str
    status: int
    method: str
    captured_at: datetime
    payload: JsonValue
    content_type: str
    estimated_bytes: int


type PayloadPredicate = Callable[[CapturedPayload], bool]


class NetworkInterceptor:
    """Capture relevant JSON responses without blocking Playwright callbacks."""

    def __init__(
        self,
        *,
        url_patterns: Sequence[str | re.Pattern[str]] = _DEFAULT_URL_PATTERNS,
        max_payloads: int = 100,
        max_estimated_bytes: int | None = 10 * 1024 * 1024,
        redact_query_keys: frozenset[str] = _SECRET_QUERY_KEYS,
    ) -> None:
        if max_payloads <= 0:
            raise ValueError("max_payloads must be positive")
        if max_estimated_bytes is not None and max_estimated_bytes <= 0:
            raise ValueError("max_estimated_bytes must be positive when set")
        self._patterns = tuple(
            re.compile(pattern, re.IGNORECASE) if isinstance(pattern, str) else pattern
            for pattern in url_patterns
        )
        self._max_payloads = max_payloads
        self._max_bytes = max_estimated_bytes
        self._redact_keys = {key.casefold() for key in redact_query_keys}
        self._payloads: list[CapturedPayload] = []
        self._estimated_bytes = 0
        self._fingerprints: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._page: Page | None = None
        self._condition = asyncio.Condition()
        self._closed = False

    def attach(self, page: Page) -> Self:
        """Attach the response listener to exactly one Playwright page."""
        if self._closed:
            raise RuntimeError("NetworkInterceptor is closed")
        if self._page is not None:
            raise RuntimeError("NetworkInterceptor is already attached")
        self._page = page
        page.on("response", self._on_response)
        return self

    def detach(self) -> None:
        """Remove the page listener while allowing in-flight captures to finish."""
        if self._page is not None:
            self._page.remove_listener("response", self._on_response)
            self._page = None

    def snapshot(self) -> tuple[CapturedPayload, ...]:
        """Return an immutable view of captured payloads in arrival order."""
        return tuple(self._payloads)

    def drain(self) -> tuple[CapturedPayload, ...]:
        """Return and clear captured payloads and deduplication state."""
        payloads = self.snapshot()
        self._payloads.clear()
        self._fingerprints.clear()
        self._estimated_bytes = 0
        return payloads

    async def wait_for_payload(
        self,
        *,
        timeout: float | None = None,
        predicate: PayloadPredicate | None = None,
    ) -> CapturedPayload:
        """Wait for and return the first payload matching an optional predicate."""
        matches = await self.wait_for_payloads(count=1, timeout=timeout, predicate=predicate)
        return matches[0]

    async def wait_for_payloads(
        self,
        *,
        count: int = 1,
        timeout: float | None = None,
        predicate: PayloadPredicate | None = None,
    ) -> tuple[CapturedPayload, ...]:
        """Wait until at least ``count`` retained payloads match the predicate."""
        if count <= 0:
            raise ValueError("count must be positive")

        def matching() -> tuple[CapturedPayload, ...]:
            return tuple(item for item in self._payloads if predicate is None or predicate(item))

        async def wait() -> tuple[CapturedPayload, ...]:
            async with self._condition:
                await self._condition.wait_for(lambda: len(matching()) >= count)
                return matching()

        results = await asyncio.wait_for(wait(), timeout=timeout)
        return results[:count]

    async def aclose(self) -> None:
        """Detach, cancel unfinished body reads, and await all callback tasks."""
        if self._closed:
            return
        self._closed = True
        self.detach()
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> Self:
        if self._closed:
            raise RuntimeError("NetworkInterceptor is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    def _on_response(self, response: Response) -> None:
        if self._closed or not self._is_relevant(response):
            return
        task = asyncio.create_task(self._capture(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _is_relevant(self, response: Response) -> bool:
        if not 200 <= response.status < 300:
            return False
        request = response.request
        url_match = any(pattern.search(response.url) for pattern in self._patterns)
        resource_match = request.resource_type in {"xhr", "fetch"}
        content_type = self._content_type(response.headers)
        json_type = "json" in content_type or "graphql" in content_type
        return (url_match or resource_match) and (json_type or url_match)

    async def _capture(self, response: Response) -> None:
        try:
            value = await response.json()
        except asyncio.CancelledError:
            raise
        except (PlaywrightError, ValueError, TypeError):
            return
        payload = self._validate_json(value)
        if payload is None and value is not None:
            return
        url = self._redact_url(response.url)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        estimated_bytes = len(encoded.encode("utf-8"))
        fingerprint = hashlib.sha256(
            f"{response.request.method}\0{url}\0{response.status}\0{encoded}".encode()
        ).hexdigest()
        if fingerprint in self._fingerprints:
            return
        captured = CapturedPayload(
            url=url,
            status=response.status,
            method=response.request.method,
            captured_at=datetime.now(UTC),
            payload=payload,
            content_type=self._content_type(response.headers),
            estimated_bytes=estimated_bytes,
        )
        async with self._condition:
            self._payloads.append(captured)
            self._fingerprints.add(fingerprint)
            self._estimated_bytes += estimated_bytes
            self._trim()
            self._condition.notify_all()

    def _trim(self) -> None:
        while len(self._payloads) > self._max_payloads or (
            self._max_bytes is not None and self._estimated_bytes > self._max_bytes
        ):
            removed = self._payloads.pop(0)
            self._estimated_bytes -= removed.estimated_bytes
            self._fingerprints.remove(self._fingerprint(removed))

    @staticmethod
    def _fingerprint(item: CapturedPayload) -> str:
        encoded = json.dumps(
            item.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(
            f"{item.method}\0{item.url}\0{item.status}\0{encoded}".encode()
        ).hexdigest()

    def _redact_url(self, url: str) -> str:
        parts = urlsplit(url)
        query = urlencode(
            [
                (key, "[REDACTED]" if key.casefold() in self._redact_keys else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    @staticmethod
    def _content_type(headers: Mapping[str, str]) -> str:
        return headers.get("content-type", headers.get("Content-Type", "")).casefold()

    @staticmethod
    def _validate_json(value: object) -> JsonValue | None:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, list):
            items: list[JsonValue] = []
            for item in value:
                validated = NetworkInterceptor._validate_json(item)
                if validated is None and item is not None:
                    return None
                items.append(validated)
            return items
        if isinstance(value, dict) and all(isinstance(key, str) for key in value):
            result: dict[str, JsonValue] = {}
            for key, item in value.items():
                validated = NetworkInterceptor._validate_json(item)
                if validated is None and item is not None:
                    return None
                result[str(key)] = validated
            return result
        return None


__all__ = ["CapturedPayload", "NetworkInterceptor", "PayloadPredicate"]
