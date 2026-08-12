"""Typed asynchronous client for the GitHub commits REST API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Self, cast

import httpx

GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
DEFAULT_USER_AGENT = "JobPing/0.1"
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class GitHubClientError(RuntimeError):
    """Base error raised for GitHub API and transport failures."""


class GitHubNotFoundError(GitHubClientError):
    """Requested repository or commit was not found."""


class GitHubRateLimitError(GitHubClientError):
    """GitHub rejected a request because a rate limit was reached."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None,
        limit: int | None,
        remaining: int | None,
        reset_at: datetime | None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.limit = limit
        self.remaining = remaining
        self.reset_at = reset_at


@dataclass(frozen=True, slots=True)
class GitHubCommitSummary:
    """Commit metadata returned by the repository commits endpoint."""

    sha: str
    message: str
    html_url: str
    authored_at: datetime | None


@dataclass(frozen=True, slots=True)
class GitHubFilePatch:
    """A Markdown file changed by a commit, including its optional patch."""

    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None


@dataclass(frozen=True, slots=True)
class GitHubCommitDetail:
    """Commit metadata and changed target Markdown files."""

    sha: str
    message: str
    html_url: str
    authored_at: datetime | None
    files: tuple[GitHubFilePatch, ...]


class GitHubClient:
    """Fetch GitHub commit metadata while respecting client ownership."""

    def __init__(
        self,
        *,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = GITHUB_API_URL,
        timeout: httpx.Timeout | float = 10.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        resolved_token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": user_agent,
        }
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )
        self._request_headers = headers if client is not None else {}
        self._timeout = timeout
        self._closed = False

    async def list_commits(
        self,
        owner: str,
        repo: str,
        *,
        ref: str | None = None,
        path: str | None = None,
        since: datetime | None = None,
        page: int = 1,
        per_page: int = 30,
    ) -> tuple[GitHubCommitSummary, ...]:
        """Return one page of repository commits, optionally narrowed by path/ref/time."""
        if page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= per_page <= 100:
            raise ValueError("per_page must be between 1 and 100")
        params: dict[str, str | int] = {"page": page, "per_page": per_page}
        if ref is not None:
            params["sha"] = ref
        if path is not None:
            params["path"] = path
        if since is not None:
            params["since"] = since.isoformat()
        payload = await self._get_json(f"/repos/{owner}/{repo}/commits", params=params)
        if not isinstance(payload, list):
            raise GitHubClientError("GitHub returned an invalid commits response")
        return tuple(self._parse_summary(item) for item in payload)

    async def get_commit(
        self,
        owner: str,
        repo: str,
        ref: str | None = None,
        *,
        target_markdown_paths: set[str] | None = None,
    ) -> GitHubCommitDetail:
        """Return commit detail, resolving the latest commit when ref is absent."""
        resolved_ref = ref
        if not resolved_ref:
            commits = await self.list_commits(owner, repo, per_page=1)
            if not commits:
                raise GitHubNotFoundError(f"GitHub repository has no commits: {owner}/{repo}")
            resolved_ref = commits[0].sha
        payload = await self._get_json(f"/repos/{owner}/{repo}/commits/{resolved_ref}")
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub returned an invalid commit response")
        files_payload = payload.get("files", [])
        if not isinstance(files_payload, list):
            raise GitHubClientError("GitHub returned invalid commit files")
        files = tuple(
            self._parse_file(item)
            for item in files_payload
            if isinstance(item, dict)
            and self._is_target_markdown(str(item.get("filename", "")), target_markdown_paths)
        )
        summary = self._parse_summary(payload)
        return GitHubCommitDetail(
            sha=summary.sha,
            message=summary.message,
            html_url=summary.html_url,
            authored_at=summary.authored_at,
            files=files,
        )

    async def aclose(self) -> None:
        """Close the underlying client only when this instance created it."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        await self.aclose()

    async def _get_json(
        self, path: str, *, params: Mapping[str, str | int] | None = None
    ) -> JsonValue:
        if self._closed:
            raise RuntimeError("GitHubClient is closed")
        try:
            response = await self._client.get(
                path,
                params=params,
                headers=self._request_headers,
                timeout=self._timeout,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise GitHubClientError(f"GitHub request failed: {exc}") from exc
        if response.status_code in {403, 429}:
            raise self._rate_limit_error(response)
        if response.status_code == 404:
            raise GitHubNotFoundError(f"GitHub resource not found: {path}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubClientError(
                f"GitHub returned HTTP {response.status_code} for {path}"
            ) from exc
        try:
            return cast(JsonValue, response.json())
        except ValueError as exc:
            raise GitHubClientError("GitHub returned an invalid JSON response") from exc

    @staticmethod
    def _parse_summary(payload: object) -> GitHubCommitSummary:
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub returned invalid commit metadata")
        commit = payload.get("commit")
        if not isinstance(commit, dict):
            raise GitHubClientError("GitHub commit metadata is missing")
        author = commit.get("author")
        authored_at = None
        if isinstance(author, dict) and isinstance(author.get("date"), str):
            try:
                authored_at = datetime.fromisoformat(author["date"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise GitHubClientError("GitHub returned an invalid commit date") from exc
        try:
            return GitHubCommitSummary(
                sha=str(payload["sha"]),
                message=str(commit["message"]),
                html_url=str(payload["html_url"]),
                authored_at=authored_at,
            )
        except KeyError as exc:
            raise GitHubClientError(f"GitHub commit metadata is missing {exc.args[0]}") from exc

    @staticmethod
    def _parse_file(payload: Mapping[str, object]) -> GitHubFilePatch:
        try:
            return GitHubFilePatch(
                filename=str(payload["filename"]),
                status=str(payload["status"]),
                additions=int(payload.get("additions", 0)),
                deletions=int(payload.get("deletions", 0)),
                changes=int(payload.get("changes", 0)),
                patch=str(payload["patch"]) if payload.get("patch") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubClientError("GitHub returned invalid file metadata") from exc

    @staticmethod
    def _is_target_markdown(filename: str, targets: set[str] | None) -> bool:
        is_markdown = filename.casefold().endswith((".md", ".mdx"))
        return is_markdown and (targets is None or filename in targets)

    @staticmethod
    def _rate_limit_error(response: httpx.Response) -> GitHubRateLimitError:
        headers = response.headers
        retry_after: float | None = None
        raw_retry_after = headers.get("Retry-After")
        if raw_retry_after is not None:
            try:
                retry_after = max(0.0, float(raw_retry_after))
            except ValueError:
                try:
                    retry_date = parsedate_to_datetime(raw_retry_after)
                    retry_after = max(
                        0.0, (retry_date - datetime.now(retry_date.tzinfo)).total_seconds()
                    )
                except (TypeError, ValueError, OverflowError):
                    retry_after = None
        limit = GitHubClient._optional_int(headers.get("X-RateLimit-Limit"))
        remaining = GitHubClient._optional_int(headers.get("X-RateLimit-Remaining"))
        reset_timestamp = GitHubClient._optional_int(headers.get("X-RateLimit-Reset"))
        try:
            reset_at = (
                datetime.fromtimestamp(reset_timestamp, tz=UTC)
                if reset_timestamp is not None
                else None
            )
        except (OSError, OverflowError, ValueError):
            reset_at = None
        return GitHubRateLimitError(
            f"GitHub rate limit response (HTTP {response.status_code})",
            retry_after_seconds=retry_after,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
        )

    @staticmethod
    def _optional_int(value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None
