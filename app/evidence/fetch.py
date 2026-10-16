"""Public-only bounded retrieval with DNS pinned at the actual HTTP connection."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


class FetchError(RuntimeError):
    """Fixed codes only: no untrusted URLs, headers, or secrets in errors."""


@dataclass(frozen=True)
class Page:
    url: str
    body: bytes
    content_type: str
    headers: dict[str, str]


class PublicTransport(httpx.AsyncBaseTransport):
    """Resolve once, reject any private address, connect to that IP with original TLS SNI."""

    def __init__(self) -> None:
        self.transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if request.url.scheme not in {"http", "https"} or port not in {80, 443}:
            raise FetchError("unsupported_destination")
        if request.url.userinfo:
            raise FetchError("credentials_in_url")
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError:
            raise FetchError("dns_unavailable") from None
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise FetchError("non_public_destination")
        pinned = request.url.copy_with(host=addresses[0][4][0])
        headers = request.headers.copy()
        headers["host"] = host
        extension = {**request.extensions, "sni_hostname": host}
        forwarded = httpx.Request(
            request.method, pinned, headers=headers, stream=request.stream, extensions=extension
        )
        response = await self.transport.handle_async_request(forwarded)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


class Fetcher:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self._domains: dict[str, asyncio.Lock] = {}
        self._cooldowns: dict[str, float] = {}

    async def get(self, url: str, *, max_bytes: int = 2_000_000) -> Page:
        for _ in range(6):
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.port not in {None, 80, 443}
                or len(url) > 2048
            ):
                raise FetchError("unsupported_destination")
            domain = parts.hostname.casefold()
            async with self._domains.setdefault(domain, asyncio.Lock()):
                if self._cooldowns.get(domain, 0) > asyncio.get_running_loop().time():
                    raise FetchError("provider_cooldown")
                for attempt in range(3):
                    try:
                        # No cookies or authorization propagate between sources.
                        request = self.client.build_request(
                            "GET",
                            url,
                            timeout=10,
                            headers={
                                "User-Agent": "JobPing evidence checker",
                                "Accept": "text/html,application/json",
                            },
                        )
                        request.headers.pop("cookie", None)
                        request.headers.pop("authorization", None)
                        response = await self.client.send(
                            request, stream=True, follow_redirects=False
                        )
                        try:
                            if response.status_code in {429, 503}:
                                self._cooldowns[domain] = asyncio.get_running_loop().time() + 60
                                raise FetchError("provider_cooldown")
                            if response.status_code >= 500 and attempt < 2:
                                await asyncio.sleep(0.25 * (2**attempt))
                                continue
                            if response.is_redirect:
                                destination = response.headers.get("location")
                                if not destination:
                                    raise FetchError("invalid_redirect")
                                url = urljoin(url, destination)
                                break
                            if response.status_code != 200:
                                raise FetchError("http_unavailable")
                            content = bytearray()
                            async for chunk in response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > max_bytes:
                                    raise FetchError("document_too_large")
                            return Page(
                                str(response.url),
                                bytes(content),
                                response.headers.get("content-type", ""),
                                {
                                    key: value
                                    for key, value in response.headers.items()
                                    if key.lower() in {"content-type", "content-security-policy"}
                                },
                            )
                        finally:
                            await response.aclose()
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise FetchError("transport_unavailable") from None
                        await asyncio.sleep(0.25 * (2**attempt))
                else:
                    raise FetchError("transport_unavailable")
        raise FetchError("too_many_redirects")
