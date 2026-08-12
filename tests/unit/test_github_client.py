"""Tests for the GitHub REST API client using an in-memory HTTP transport."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from app.scrapers.github_client import (
    GitHubClient,
    GitHubClientError,
    GitHubNotFoundError,
    GitHubRateLimitError,
)


def commit_payload(*, files: list[dict[str, object]] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "sha": "abc123",
        "html_url": "https://github.com/acme/jobs/commit/abc123",
        "commit": {"message": "update roles", "author": {"date": "2026-08-11T12:00:00Z"}},
    }
    if files is not None:
        payload["files"] = files
    return payload


@pytest.mark.asyncio
async def test_list_commits_sends_filters_headers_and_parses_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/jobs/commits"
        assert dict(request.url.params) == {
            "page": "2",
            "per_page": "50",
            "sha": "main",
            "path": "README.md",
            "since": "2026-08-01T00:00:00+00:00",
        }
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.headers["User-Agent"] == "JobPing/0.1"
        return httpx.Response(200, json=[commit_payload()])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        client = GitHubClient(client=http, token="secret")
        commits = await client.list_commits(
            "acme",
            "jobs",
            ref="main",
            path="README.md",
            since=datetime(2026, 8, 1, tzinfo=UTC),
            page=2,
            per_page=50,
        )
        await client.aclose()
        assert not http.is_closed
    assert commits[0].sha == "abc123"
    assert commits[0].authored_at == datetime(2026, 8, 11, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_commit_filters_target_markdown_patches() -> None:
    payload = commit_payload(
        files=[
            {
                "filename": "README.md",
                "status": "modified",
                "additions": 2,
                "deletions": 1,
                "changes": 3,
                "patch": "@@ roles",
            },
            {"filename": "archive.md", "status": "modified", "changes": 1},
            {"filename": "script.py", "status": "modified", "patch": "ignored"},
        ]
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        detail = await GitHubClient(client=http).get_commit(
            "acme", "jobs", "abc123", target_markdown_paths={"README.md"}
        )
    assert [file.filename for file in detail.files] == ["README.md"]
    assert detail.files[0].patch == "@@ roles"


@pytest.mark.asyncio
async def test_get_commit_without_ref_resolves_latest_commit_sha() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/acme/jobs/commits":
            assert request.url.params["per_page"] == "1"
            return httpx.Response(200, json=[commit_payload()])
        assert request.url.path == "/repos/acme/jobs/commits/abc123"
        return httpx.Response(200, json=commit_payload(files=[]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        detail = await GitHubClient(client=http).get_commit("acme", "jobs")

    assert detail.sha == "abc123"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_requests_follow_repository_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/old/jobs/commits/main":
            return httpx.Response(
                301,
                headers={"Location": "https://api.github.test/repos/new/jobs/commits/main"},
            )
        assert request.url.path == "/repos/new/jobs/commits/main"
        return httpx.Response(200, json=commit_payload(files=[]))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        client = GitHubClient(client=http)
        detail = await client.get_commit("old", "jobs", "main")
    assert detail.sha == "abc123"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
async def test_rate_limit_headers_are_exposed(status: int) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            headers={
                "Retry-After": "12",
                "X-RateLimit-Limit": "5000",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1786453200",
            },
        )
    )
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        with pytest.raises(GitHubRateLimitError) as raised:
            await GitHubClient(client=http).list_commits("acme", "jobs")
    assert raised.value.retry_after_seconds == 12
    assert raised.value.limit == 5000
    assert raised.value.remaining == 0
    assert raised.value.reset_at is not None


@pytest.mark.asyncio
async def test_rate_limit_with_missing_or_malformed_headers_is_safe() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403, headers={"Retry-After": "later", "X-RateLimit-Limit": "many"}
        )
    )
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        with pytest.raises(GitHubRateLimitError) as raised:
            await GitHubClient(client=http).list_commits("acme", "jobs")
    assert raised.value.retry_after_seconds is None
    assert raised.value.limit is None
    assert raised.value.remaining is None
    assert raised.value.reset_at is None


@pytest.mark.asyncio
async def test_not_found_has_specific_error() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        with pytest.raises(GitHubNotFoundError):
            await GitHubClient(client=http).get_commit("acme", "jobs", "missing")


@pytest.mark.asyncio
async def test_network_failure_is_mapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://api.github.test", transport=transport) as http:
        with pytest.raises(GitHubClientError, match="request failed"):
            await GitHubClient(client=http).list_commits("acme", "jobs")


@pytest.mark.asyncio
async def test_internally_owned_client_is_closed() -> None:
    client = GitHubClient()
    underlying = client._client
    await client.aclose()
    await client.aclose()
    assert underlying.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        await client.list_commits("acme", "jobs")
