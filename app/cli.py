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
    ref: str,
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


def _summary(result: PipelineResult) -> str:
    counts = " ".join(
        f"{state.value}={len(result.categorized(state))}" for state in DeduplicationState
    )
    return f"Processed commit {result.commit_sha}: {counts} rejected={len(result.rejected)}"


@app.command("run-simplify-parser")
def run_simplify_parser(
    owner: Annotated[
        str, typer.Option(envvar="GITHUB_OWNER", help="GitHub repository owner.")
    ] = "SimplifyJobs",
    repo: Annotated[
        str, typer.Option(envvar="GITHUB_REPO", help="GitHub repository name.")
    ] = "Summer2026-Internships",
    ref: Annotated[
        str, typer.Option(envvar="GITHUB_REF", help="Commit SHA, tag, or branch to process.")
    ] = "HEAD",
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
    """Fetch and classify one Simplify repository commit."""
    try:
        result = asyncio.run(
            _process_commit(
                owner=owner,
                repo=repo,
                ref=ref,
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
    typer.echo(_summary(result))


def main() -> None:
    """Invoke the Typer application when executed as a module."""
    app()


if __name__ == "__main__":
    main()
