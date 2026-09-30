"""Small in-process caches used by the API (no FastAPI imports; unit-testable).

* ``VerifiedTokenCache`` — REVIEW_AUTH_API F19 (Firebase revocation check quota).
* ``TtlCache`` — REVIEW_AUTH_API F10 (public leaderboard: expensive, anonymous, not cached by Cloudflare).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable


class VerifiedTokenCache:
    """REVIEW_AUTH_API F19: the prod verifier makes one Firebase Auth backend call per request (revocation check).
    A token that verified is remembered for at most ``ttl`` seconds (never past its own ``exp``), keyed by its
    SHA-256, so a busy session costs ~1 backend call per minute instead of one per request. Trade-off (documented):
    a token revoked meanwhile keeps working for ≤ ttl seconds; step-up freshness is still checked on every request
    from ``auth_time``. Only successes are cached (a bad token is re-checked every time)."""

    def __init__(self, ttl: float = 60.0, size: int = 20_000, clock: Callable[[], float] = time.time) -> None:
        self._ttl, self._size, self._clock = float(ttl), int(size), clock
        self._d: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str) -> Any:
        k, now = self.key(token), self._clock()
        with self._lock:
            hit = self._d.get(k)
            if hit is None:
                return None
            if hit[0] <= now:
                self._d.pop(k, None)
                return None
            self._d.move_to_end(k)
            return hit[1]

    def put(self, token: str, value: Any, exp: Any) -> None:
        now = self._clock()
        until = now + self._ttl
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            until = min(until, float(exp))
        if until <= now:
            return
        with self._lock:
            self._d[self.key(token)] = (until, value)
            self._d.move_to_end(self.key(token))
            while len(self._d) > self._size:
                self._d.popitem(last=False)


class TtlCache:
    """REVIEW_AUTH_API F10: per-instance cache of the leaderboard (60 s), single-flight per key so a burst of
    anonymous requests computes it once (Cloudflare does not cache the API host)."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        import threading
        self.ttl = ttl_seconds
        self._d: dict[Any, tuple[float, Any]] = {}
        self._locks: dict[Any, Any] = {}
        self._guard = threading.Lock()

    def get_or_compute(self, key: Any, compute: Any) -> Any:
        import threading
        import time
        now = time.monotonic()
        hit = self._d.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._d.get(key)
            if hit is not None and hit[0] > time.monotonic():
                return hit[1]
            value = compute()
            self._d[key] = (time.monotonic() + self.ttl, value)
            return value

    def clear(self) -> None:
        self._d.clear()
