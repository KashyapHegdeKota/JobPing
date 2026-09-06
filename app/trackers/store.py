"""Atomic local tracker documents with exclusive per-tracker operations."""

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from app.trackers.models import Tracker
from app.trackers.service import TrackerError


class TrackerStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, tracker_id: str) -> Path:
        if re.fullmatch(r"[a-f0-9]{32}", tracker_id) is None:
            raise TrackerError("Invalid tracker ID")
        return self.root / f"{tracker_id}.json"

    def load(self, tracker_id: str) -> Tracker:
        tracker = Tracker.model_validate_json(self.path(tracker_id).read_text(encoding="utf-8"))
        if tracker.id != tracker_id:
            raise TrackerError("Tracker ID does not match its file")
        return tracker

    def save(self, tracker: Tracker) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(tracker.id)
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(tracker.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def locked(self, tracker_id: str) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.path(tracker_id).with_suffix(".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise TrackerError(
                "Tracker is busy (or has a stale .lock file after a crash)"
            ) from None
        try:
            os.close(descriptor)
            yield
        finally:
            lock.unlink(missing_ok=True)
