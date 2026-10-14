"""Linux production singleton lock and heartbeat around the existing CLI workers."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from app import cli


async def run(role: str) -> None:
    import fcntl

    directory = Path("/run/jobping-workers")
    with (directory / f"{role}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        loop = asyncio.get_running_loop()
        # The scheduler owns SIGTERM draining; SIGINT retains normal cancellation.
        if role == "notifications":
            from app.notifications.worker import serve

            task = asyncio.create_task(serve())
        elif role == "evidence":
            from app.evidence.worker import serve

            task = asyncio.create_task(serve())
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        else:
            sources = cli.load_ats_sources(
                Path(os.environ["ATS_SOURCES_FILE"]) if os.environ.get("ATS_SOURCES_FILE") else None
            )
            task = asyncio.create_task(
                cli._serve_scheduler(
                    [
                        "github.com=60",
                        "applyguy.ai=120",
                        "boards.greenhouse.io=120",
                        "api.lever.co=120",
                    ],
                    redis_url=os.environ["REDIS_URL"],
                    github_token=os.environ.get("GITHUB_TOKEN"),
                    database_url=os.environ["DATABASE_URL"],
                    ats_sources=sources,
                )
            )
        loop.add_signal_handler(signal.SIGINT, task.cancel)
        try:
            while not task.done():
                (directory / f"{role}.heartbeat").write_text(str(time.time()), encoding="ascii")
                await asyncio.wait({task}, timeout=5)
            await task
        except asyncio.CancelledError:
            if role != "evidence":
                raise
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            if role == "evidence":
                loop.remove_signal_handler(signal.SIGTERM)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            (directory / f"{role}.heartbeat").unlink(missing_ok=True)


def main() -> None:
    role = os.environ.get("JOBPING_WORKER", "")
    if sys.argv[1:] == ["health"]:
        age = time.time() - float(
            Path(f"/run/jobping-workers/{role}.heartbeat").read_text(encoding="ascii")
        )
        raise SystemExit(0 if 0 <= age < 30 else 1)
    if sys.argv[1:] not in (["scheduler"], ["notifications"], ["evidence"]):
        raise SystemExit("Expected scheduler, notifications, evidence or health")
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()
