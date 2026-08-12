"""Command-line entry points for local JobPing ingestion runs."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from app.pipelines.simplify_pipeline import PipelineResult, SimplifyPipeline
from app.schemas.job import JobType
from app.scrapers.github_client import GitHubClient
from app.services.deduplicator import DeduplicationState, JobDeduplicator

app = typer.Typer(help="Run JobPing ingestion tools.", no_args_is_help=True)


@app.callback()
def cli_root() -> None:
    """Run JobPing ingestion tools."""


async def _process_commit(
    *,
    owner: str,
    repo: str,
    ref: str | None,
    target_readme: str,
    season: int,
    job_type: JobType,
    redis_url: str,
    github_token: str | None,
) -> PipelineResult:
    """Construct owned clients, process one commit, and release resources."""
    async with GitHubClient(token=github_token) as github:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            pipeline = SimplifyPipeline(
                github,
                deduplicator,
                season=season,
                job_type=job_type,
                target_readme_paths={target_readme},
            )
            return await pipeline.process_commit(owner, repo, ref)


async def _process_commits(
    *,
    owner: str,
    repo: str,
    commit_sha: str | None,
    limit: int,
    target_readme: str,
    season: int,
    job_type: JobType,
    redis_url: str,
    github_token: str | None,
) -> tuple[PipelineResult, ...]:
    """Process one explicit commit or a bounded batch of recent commits."""
    async with GitHubClient(token=github_token) as github:
        async with JobDeduplicator.from_url(redis_url) as deduplicator:
            pipeline = SimplifyPipeline(
                github,
                deduplicator,
                season=season,
                job_type=job_type,
                target_readme_paths={target_readme},
            )
            if commit_sha:
                refs = (commit_sha,)
            else:
                commits = await github.list_commits(owner, repo, per_page=limit)
                refs = tuple(commit.sha for commit in commits)
            return tuple([await pipeline.process_commit(owner, repo, ref) for ref in refs])


def _summary(result: PipelineResult) -> str:
    counts = " ".join(
        f"{state.value}={len(result.categorized(state))}" for state in DeduplicationState
    )
    return f"Processed commit {result.commit_sha}: {counts} rejected={len(result.rejected)}"


def _job_lines(result: PipelineResult) -> tuple[str, ...]:
    """Return concise company/title lines for parsed, non-no-op changes."""
    return tuple(
        _console_safe(f"  [{item.state.value}] {item.job.company_name} - {item.job.title}")
        for state in (
            DeduplicationState.NEW_ROLE,
            DeduplicationState.ROLE_UPDATED,
            DeduplicationState.ROLE_CLOSED,
            DeduplicationState.NO_OP,
        )
        for item in result.categorized(state)
    )


def _console_safe(value: str) -> str:
    """Make live job summaries printable on legacy Windows terminals."""
    import sys

    encoding = sys.stdout.encoding or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding)


@app.command("run-simplify-parser")
def run_simplify_parser(
    owner: Annotated[
        str, typer.Option(envvar="GITHUB_OWNER", help="GitHub repository owner.")
    ] = "SimplifyJobs",
    repo: Annotated[
        str, typer.Option(envvar="GITHUB_REPO", help="GitHub repository name.")
    ] = "Summer2026-Internships",
    commit_sha: Annotated[
        str | None,
        typer.Option(
            "--commit-sha",
            "--ref",
            envvar="GITHUB_REF",
            help="Specific commit SHA, tag, or branch to process.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(min=1, max=100, help="Recent commits to scan when no SHA is given."),
    ] = 1,
    target_readme: Annotated[
        str, typer.Option(envvar="TARGET_README", help="Markdown path to inspect.")
    ] = "README.md",
    season: Annotated[
        int, typer.Option(min=2026, max=2027, envvar="JOB_SEASON", help="Hiring season.")
    ] = 2026,
    job_type: Annotated[
        JobType, typer.Option(envvar="JOB_TYPE", help="Job category to assign.")
    ] = JobType.INTERNSHIP,
    redis_url: Annotated[
        str, typer.Option(envvar="REDIS_URL", help="Redis connection URL.")
    ] = "redis://localhost:6379/0",
    github_token: Annotated[
        str | None,
        typer.Option(envvar="GITHUB_TOKEN", help="Optional GitHub API token.", hidden=True),
    ] = None,
) -> None:
    """Fetch and classify one or more Simplify repository commits."""
    try:
        results = asyncio.run(
            _process_commits(
                owner=owner,
                repo=repo,
                commit_sha=commit_sha,
                limit=limit,
                target_readme=target_readme,
                season=season,
                job_type=job_type,
                redis_url=redis_url,
                github_token=github_token,
            )
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("Simplify parser cancelled.", err=True)
        raise typer.Exit(code=130) from None
    except Exception as exc:
        typer.echo(f"Simplify parser failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for result in results:
        typer.echo(_summary(result))
        for line in _job_lines(result):
            typer.echo(line)


def main() -> None:
    """Invoke the Typer application when executed as a module."""
    app()


if __name__ == "__main__":
    main()
