"""End-to-end orchestration for Simplify README commit patches."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from app.db.repository import DatabaseRepository
from app.schemas.job import JobType, NormalizedJob, RawJobPayload
from app.scrapers.git_patch_parser import ChangedLine, ChangeKind, GitPatchParser
from app.scrapers.github_client import (
    GitHubClient,
    GitHubCommitDetail,
)
from app.scrapers.markdown_parser import MarkdownTableParser, coalesce_html_table_rows
from app.services.deduplicator import DeduplicationState, JobDeduplicator
from app.services.hasher import generate_base_hash, generate_content_hash


@dataclass(frozen=True, slots=True)
class PipelineItem:
    """One classified job with source and commit provenance."""

    state: DeduplicationState
    job: NormalizedJob
    raw: RawJobPayload
    commit_sha: str
    commit_url: str
    filename: str
    change_kind: ChangeKind


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A row skipped because it could not become a valid job."""

    filename: str
    content: str
    change_kind: ChangeKind
    reason: str


@dataclass(slots=True)
class PipelineResult:
    """Commit processing results grouped by deduplication state."""

    commit_sha: str
    items: dict[DeduplicationState, list[PipelineItem]] = field(
        default_factory=lambda: {state: [] for state in DeduplicationState}
    )
    rejected: list[RejectedRow] = field(default_factory=list)

    def categorized(self, state: DeduplicationState) -> tuple[PipelineItem, ...]:
        """Return immutable results for one state."""
        return tuple(self.items[state])


class SimplifyPipeline:
    """Fetch, parse, normalize, hash, and atomically classify README changes."""

    def __init__(
        self,
        github: GitHubClient,
        deduplicator: JobDeduplicator,
        *,
        season: int,
        job_type: JobType,
        target_readme_paths: set[str] | None = None,
    ) -> None:
        if season not in {2026, 2027}:
            raise ValueError("season must be 2026 or 2027")
        self._github = github
        self._deduplicator = deduplicator
        self._season = season
        self._job_type = job_type
        self._targets = target_readme_paths

    async def process_commit(self, owner: str, repo: str, ref: str | None = None) -> PipelineResult:
        """Fetch and process one commit reference."""
        detail = await self._github.get_commit(
            owner, repo, ref, target_markdown_paths=self._targets
        )
        return await self.process_detail(detail)

    async def process_detail(self, detail: GitHubCommitDetail) -> PipelineResult:
        """Process an already-fetched commit detail without external persistence."""
        result = PipelineResult(commit_sha=detail.sha)
        candidates: list[tuple[RawJobPayload, ChangeKind, str]] = []
        for parsed_patch in GitPatchParser.parse_files(
            detail.files, target_readme_paths=self._targets
        ):
            filename = parsed_patch.filename or "README.md"
            parser = MarkdownTableParser(
                source="simplify_github", source_id=f"{detail.sha}:{filename}"
            )
            for line in coalesce_html_table_rows(parsed_patch.lines):
                try:
                    raw = parser.parse(line.content)
                except (ValidationError, ValueError) as exc:
                    result.rejected.append(RejectedRow(filename, line.content, line.kind, str(exc)))
                    continue
                if raw is None:
                    if line.content.lstrip().startswith("|"):
                        result.rejected.append(
                            RejectedRow(filename, line.content, line.kind, "not a valid job row")
                        )
                    continue
                candidates.append((raw, line.kind, filename))

        await self._classify_candidates(result, candidates, detail.sha, detail.html_url)
        return result

    async def _classify_candidates(
        self,
        result: PipelineResult,
        candidates: list[tuple[RawJobPayload, ChangeKind, str]],
        source_sha: str,
        source_url: str,
    ) -> None:
        """Normalize and classify parsed rows from either diffs or raw files."""

        added_identities = {
            generate_base_hash(raw.company or "", raw.title or "")
            for raw, kind, _ in candidates
            if kind is ChangeKind.ADDED
        }
        seen: set[tuple[str, str]] = set()
        for raw, kind, filename in sorted(
            candidates, key=lambda item: item[1] is ChangeKind.REMOVED
        ):
            base_hash = generate_base_hash(raw.company or "", raw.title or "")
            if kind is ChangeKind.REMOVED and base_hash in added_identities:
                continue
            is_closed = bool(raw.is_closed) or kind is ChangeKind.REMOVED
            try:
                content_hash = generate_content_hash(
                    base_hash, raw.apply_url or "", str(raw.location or ""), is_closed
                )
                job = NormalizedJob(
                    company_name=raw.company,
                    title=raw.title or "",
                    base_hash=base_hash,
                    content_hash=content_hash,
                    apply_url=raw.apply_url,
                    location=str(raw.location or ""),
                    season=self._season,
                    job_type=self._job_type,
                    is_closed=is_closed,
                )
            except (ValidationError, ValueError) as exc:
                result.rejected.append(RejectedRow(filename, str(raw.payload), kind, str(exc)))
                continue
            signature = (base_hash, content_hash)
            if signature in seen:
                continue
            seen.add(signature)
            state = await self._deduplicator.classify_and_update(
                base_hash=base_hash, content_hash=content_hash, is_closed=is_closed
            )
            item = PipelineItem(state, job, raw, source_sha, source_url, filename, kind)
            result.items[state].append(item)

    async def process_full_sync(
        self, owner: str, repo: str, *, path: str = "README.md", ref: str = "main"
    ) -> PipelineResult:
        """Parse and classify the raw current file without commit-diff processing."""
        source = await self._github.get_file_text(owner, repo, path, ref=ref)
        result = PipelineResult(commit_sha=source.sha)
        parser = MarkdownTableParser(source="simplify_github", source_id=f"{source.sha}:{path}")
        candidates: list[tuple[RawJobPayload, ChangeKind, str]] = []
        raw_lines = tuple(ChangedLine(ChangeKind.ADDED, line) for line in source.text.splitlines())
        for line in coalesce_html_table_rows(raw_lines):
            try:
                raw = parser.parse(line.content)
            except (ValidationError, ValueError) as exc:
                result.rejected.append(RejectedRow(path, line.content, line.kind, str(exc)))
                continue
            if raw is None:
                if line.content.lstrip().startswith("|"):
                    result.rejected.append(
                        RejectedRow(path, line.content, line.kind, "not a valid job row")
                    )
                continue
            candidates.append((raw, line.kind, path))
        await self._classify_candidates(
            result,
            candidates,
            source.sha,
            f"https://github.com/{owner}/{repo}/blob/{ref}/{path}",
        )
        return result

    async def process_full_sync_files(
        self,
        owner: str,
        repo: str,
        paths: tuple[str, ...],
        *,
        ref: str = "main",
    ) -> tuple[PipelineResult, ...]:
        """Fetch and parse each current Markdown target directly from a branch."""
        normalized_paths = tuple(dict.fromkeys(path.strip() for path in paths if path.strip()))
        if not normalized_paths:
            raise ValueError("at least one target Markdown path is required")
        return tuple(
            [
                await self.process_full_sync(owner, repo, path=path, ref=ref)
                for path in normalized_paths
            ]
        )

    @staticmethod
    async def persist_results(
        repository: DatabaseRepository, results: tuple[PipelineResult, ...]
    ) -> int:
        """Persist classified rows through the repository's batch fast path."""
        jobs = [
            item.job
            for result in results
            for state in DeduplicationState
            for item in result.categorized(state)
        ]
        # A batch spanning commits can contain the same identity repeatedly. Keep
        # the final observed state while retaining deterministic first-seen order.
        latest = {job.base_hash: job for job in jobs}
        ordered = list(dict.fromkeys(job.base_hash for job in jobs))
        persisted = await repository.bulk_upsert_job_postings(
            [latest[base_hash] for base_hash in ordered]
        )
        return len(persisted)
