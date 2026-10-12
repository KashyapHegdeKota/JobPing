"""Validated rollout configuration; secrets never appear in representations."""

import json
import os
from dataclasses import dataclass, field
from typing import Literal

from app.request_limits.policies import DEFAULT_POLICIES, Policy


@dataclass(frozen=True)
class LimitSettings:
    mode: Literal["off", "shadow", "enforce"] = "off"
    secret: str = field(default="", repr=False)
    policies: dict[str, Policy] = field(default_factory=lambda: dict(DEFAULT_POLICIES))
    timeout: float = 0.25
    streams_per_ip: int = 20
    streams_total: int = 200
    lease_seconds: int = 60
    cache_seconds: int = 30

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "enforce"}:
            raise ValueError("REQUEST_LIMIT_MODE must be off, shadow or enforce")
        if self.mode != "off" and len(self.secret.encode()) < 32:
            raise ValueError("Request limits require a secret of at least 32 bytes")
        if (
            not 0 < self.timeout <= 5
            or not 1 <= self.streams_per_ip <= 10_000
            or not 1 <= self.streams_total <= 100_000
            or not 6 <= self.lease_seconds <= 300
            or not 0 <= self.cache_seconds <= 60
            or set(self.policies) != set(DEFAULT_POLICIES)
        ):
            raise ValueError("Invalid request limit configuration")

    @classmethod
    def from_env(cls) -> "LimitSettings":
        try:
            overrides = json.loads(os.environ.get("REQUEST_LIMIT_POLICIES", "{}"))
            if not isinstance(overrides, dict) or set(overrides) - set(DEFAULT_POLICIES):
                raise ValueError
            policies = dict(DEFAULT_POLICIES)
            for name, values in overrides.items():
                if not isinstance(values, dict) or set(values) != {"rate", "burst"}:
                    raise ValueError
                if type(values["burst"]) is not int:
                    raise ValueError
                policies[name] = Policy(float(values["rate"]), values["burst"])
            return cls(
                mode=os.environ.get("REQUEST_LIMIT_MODE", "off"),  # type: ignore[arg-type]
                secret=os.environ.get("REQUEST_LIMIT_SECRET", ""),
                policies=policies,
                timeout=float(os.environ.get("REQUEST_LIMIT_TIMEOUT", "0.25")),
                streams_per_ip=int(os.environ.get("REQUEST_LIMIT_STREAMS_PER_IP", "20")),
                streams_total=int(os.environ.get("REQUEST_LIMIT_STREAMS_TOTAL", "200")),
                lease_seconds=int(os.environ.get("REQUEST_LIMIT_LEASE_SECONDS", "60")),
                cache_seconds=int(os.environ.get("REQUEST_LIMIT_CACHE_SECONDS", "30")),
            )
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Invalid request limit configuration; check server settings") from None
