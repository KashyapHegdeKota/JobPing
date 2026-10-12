"""Fixed operation families keep counters and metrics bounded."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Policy:
    rate: float
    burst: int

    def __post_init__(self) -> None:
        if not isfinite(self.rate) or self.rate <= 0 or not 1 <= self.burst <= 100_000:
            raise ValueError("Invalid request limit policy")


DEFAULT_POLICIES = {
    "ingress": Policy(10, 50),
    "aggregate": Policy(100, 100),
    "public": Policy(2, 20),
    "private_read": Policy(2, 20),
    "private_write": Policy(20 / 60, 5),
    "analytics": Policy(1, 10),
    "connect": Policy(20 / 60, 5),
    "webhook": Policy(10, 50),
    "unsubscribe": Policy(1, 5),
    "preflight": Policy(5, 20),
}


def family(path: str, method: str) -> str:
    if method == "OPTIONS":
        return "preflight"
    if path in {"/api/v1/ws/live", "/api/v1/sse/feed"}:
        return "connect"
    if path.startswith("/api/v1/notifications/webhooks/"):
        return "webhook"
    if path.startswith("/api/v1/notifications/unsubscribe/"):
        return "unsubscribe"
    if path.startswith(("/api/v1/me/", "/api/v1/analytics/")):
        if path == "/api/v1/analytics/events":
            return "analytics"
        return "private_read" if method in {"GET", "HEAD"} else "private_write"
    return "public"


def private_family(value: str) -> bool:
    return value in {"private_read", "private_write", "analytics"}
