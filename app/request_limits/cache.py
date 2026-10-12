"""Bounded process-local cache for small, anonymous reference responses only."""

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CachedResponse:
    expires: float
    headers: list[tuple[bytes, bytes]]
    body: bytes


class PublicCache:
    def __init__(self, seconds: int, capacity: int = 2) -> None:
        self.seconds = seconds
        self.capacity = capacity
        self.entries: OrderedDict[str, CachedResponse] = OrderedDict()

    def get(self, path: str) -> CachedResponse | None:
        value = self.entries.get(path)
        if value is None:
            return None
        if value.expires <= time.monotonic():
            self.entries.pop(path, None)
            return None
        self.entries.move_to_end(path)
        return value

    def put(self, path: str, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        if not self.seconds or len(body) > 262_144:
            return
        if any(name.lower() == b"set-cookie" for name, _ in headers):
            return
        self.entries[path] = CachedResponse(time.monotonic() + self.seconds, headers, body)
        self.entries.move_to_end(path)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)
