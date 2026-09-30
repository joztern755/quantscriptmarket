"""Token-bucket rate limiting (SPEC §5.7: per-user and per-IP API limits).

`InMemoryRateLimiter` is per-process: on Cloud Run with N instances the effective limit is up to N x the
configured rate, and state is lost on scale-to-zero. That is acceptable as a second layer behind Cloudflare
WAF rate-limiting rules (the first, global layer — configure per-path rules for /v1/agents, /v1/withdrawals,
/v1/deposits/*, /webhooks/stripe and auth-heavy routes). For a strict shared limit in prod, implement
`RateLimiter` over Redis (Memorystore, private VPC) with an atomic Lua script (GET tokens/ts -> refill ->
decrement -> SET with PX TTL = time to full), keyed "rl:{policy}:{key}"; fail OPEN for reads and fail CLOSED
for money-moving routes if Redis is unreachable.

Keys: use the Firebase uid for authenticated routes and a peppered hash of the client IP (from Cloudflare's
CF-Connecting-IP, only trusted when the request came through Cloudflare) for anonymous ones — never raw IPs.
"""
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Protocol

from app.errors import RateLimited

__all__ = ["RateLimit", "RateLimiter", "InMemoryRateLimiter", "DEFAULT_POLICIES"]


@dataclass(frozen=True)
class RateLimit:
    capacity: int            # burst size (tokens)
    refill_per_s: float      # sustained rate (tokens / second)

    def __post_init__(self) -> None:
        if self.capacity < 1 or not self.refill_per_s > 0:
            raise ValueError("capacity >= 1 and refill_per_s > 0 required")

    @classmethod
    def per_minute(cls, n: int, burst: int | None = None) -> "RateLimit":
        return cls(capacity=burst or n, refill_per_s=n / 60.0)


# Starting points; tune from real traffic. Keys: uid (authenticated) or ip hash (anonymous).
DEFAULT_POLICIES: dict[str, RateLimit] = {
    "public_ip": RateLimit.per_minute(120, burst=60),
    "auth_user": RateLimit.per_minute(120, burst=60),
    "step_up_user": RateLimit.per_minute(10, burst=5),        # agents, subscribe/modify, withdrawals, admin
    "deposit_create_user": RateLimit.per_minute(6, burst=3),
    "webhook_ip": RateLimit.per_minute(600, burst=200),
}


class RateLimiter(Protocol):
    def try_acquire(self, key: str, cost: int = 1) -> tuple[bool, float]:
        """(allowed, retry_after_seconds). retry_after is 0 when allowed."""

    def check(self, key: str, cost: int = 1) -> None:
        """Raise RateLimited(retry_after=...) if not allowed."""


class InMemoryRateLimiter:
    """Thread-safe per-key token buckets with LRU eviction to bound memory."""

    def __init__(self, limit: RateLimit, *, clock: Callable[[], float] = time.monotonic, max_keys: int = 100_000) -> None:
        self._limit = limit
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def try_acquire(self, key: str, cost: int = 1) -> tuple[bool, float]:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        lim = self._limit
        if cost > lim.capacity:
            return False, math.inf
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.pop(key, (float(lim.capacity), now))
            tokens = min(float(lim.capacity), tokens + max(0.0, now - last) * lim.refill_per_s)
            if tokens >= cost:
                tokens -= cost
                allowed, retry = True, 0.0
            else:
                allowed, retry = False, (cost - tokens) / lim.refill_per_s
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)  # evicting = resetting to full; acceptable, bounded memory
            return allowed, retry

    def check(self, key: str, cost: int = 1) -> None:
        allowed, retry = self.try_acquire(key, cost)
        if not allowed:
            raise RateLimited("rate limit exceeded", retry_after=int(math.ceil(retry)) if math.isfinite(retry) else None)
