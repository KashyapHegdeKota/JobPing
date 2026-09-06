"""CLI for local customized tracker agents."""

import asyncio
from pathlib import Path
from typing import Annotated, Literal

import httpx
import typer
from pydantic import ValidationError

from app.trackers.models import Provider
from app.trackers.service import TrackerError, check_tracker, create_tracker
from app.trackers.store import TrackerStore

app = typer.Typer(help="Create and run read-only BYOK job tracker agents.", no_args_is_help=True)
StoreOption = Annotated[Path, typer.Option(envvar="JOBPING_TRACKER_DIR")]
DEFAULT_STORE = Path("private/trackers")


def fail(exc: Exception) -> None:
    message = (
        str(exc) if isinstance(exc, TrackerError) else "Invalid tracker settings or local file"
    )
    typer.echo(message, err=True)
    raise typer.Exit(1)


async def create_and_save(
    url: str, request: str, scope: str, provider: Provider, interval: int, store: Path
) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        tracker = await create_tracker(
            client,
            url=url,
            scope=scope,
            request=request,
            provider=provider,
            interval_seconds=interval,
        )
        baseline = await check_tracker(client, tracker)
    tracker.history.append(baseline)
    repository = TrackerStore(store)
    with repository.locked(tracker.id):
        repository.save(tracker)
    return tracker.model_dump_json(indent=2)


@app.command("create")
def create(
    url: str,
    request: Annotated[str, typer.Option(help="What jobs and changes should the agent watch?")],
    model: Annotated[str, typer.Option(envvar="JOBPING_TRACKER_MODEL")],
    scope: Literal["posting", "company"] = "posting",
    base_url: str = "https://api.openai.com/v1",
    key_env: str = "JOBPING_TRACKER_API_KEY",
    interval: Annotated[int, typer.Option(min=60, max=604800)] = 3600,
    store: StoreOption = DEFAULT_STORE,
) -> None:
    """Generate a custom plan and initial snapshot (two paid model requests)."""
    from app.cli import _asyncio_run

    try:
        provider = Provider(base_url=base_url, model=model, key_env=key_env)
        typer.echo(_asyncio_run(create_and_save(url, request, scope, provider, interval, store)))
    except (TrackerError, ValidationError, OSError, httpx.HTTPError) as exc:
        fail(exc)


async def run_checks(tracker_id: str, store: Path, watch: bool, max_checks: int) -> None:
    repository = TrackerStore(store)
    count = 0
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        while True:
            with repository.locked(tracker_id):
                tracker = repository.load(tracker_id)
                try:
                    result = await check_tracker(client, tracker)
                except TrackerError as exc:
                    if not watch:
                        raise
                    typer.echo(str(exc), err=True)
                else:
                    tracker.history = (tracker.history + [result])[-100:]
                    repository.save(tracker)
                    typer.echo(result.model_dump_json())
            count += 1
            if not watch or (max_checks and count >= max_checks):
                return
            await asyncio.sleep(tracker.interval_seconds)


@app.command("run")
def run(
    tracker_id: str,
    watch: bool = False,
    max_checks: Annotated[int, typer.Option(min=0, help="0 means unlimited in watch mode")] = 0,
    store: StoreOption = DEFAULT_STORE,
) -> None:
    """Check once, or poll in the foreground with --watch; Ctrl+C stops the agent."""
    from app.cli import _asyncio_run

    try:
        _asyncio_run(run_checks(tracker_id, store, watch, max_checks))
    except (TrackerError, ValidationError, OSError) as exc:
        fail(exc)


@app.command("show")
def show(tracker_id: str, store: StoreOption = DEFAULT_STORE) -> None:
    """Print the custom plan, snapshots, and change history as JSON."""
    try:
        typer.echo(TrackerStore(store).load(tracker_id).model_dump_json(indent=2))
    except (TrackerError, ValidationError, OSError) as exc:
        fail(exc)


@app.command("list")
def list_trackers(store: StoreOption = DEFAULT_STORE) -> None:
    """List saved tracker IDs and names without contacting the provider."""
    try:
        repository = TrackerStore(store)
        for path in sorted(store.glob("*.json")):
            tracker = repository.load(path.stem)
            typer.echo(f"{tracker.id}  {tracker.plan.name}")
    except (TrackerError, ValidationError, OSError) as exc:
        fail(exc)
