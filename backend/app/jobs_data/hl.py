"""Hyperliquid read access for the data jobs: the ``/info`` client factory and a request-weight pacer.

Rate limits (Hyperliquid docs, recalled — UNVERIFIED from this environment, SPEC §6): 1200 weight / minute / IP for
``/info``; most requests weigh 20; ``candleSnapshot`` adds 1 per 60 candles returned; ``userFills*`` / ``userFunding``
add 1 per 20 items. The executor shares the egress IP, so every job paces itself to a fraction of that budget
(``weight_per_minute``, default 600) and bounds its work per call (resumable cursors pick up the rest).
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

__all__ = ["WeightPacer", "make_info_client", "candle_weight", "list_weight", "BASE_WEIGHT"]

BASE_WEIGHT = 20


def candle_weight(n_items: int) -> int:
    return BASE_WEIGHT + math.ceil(max(0, n_items) / 60)


def list_weight(n_items: int) -> int:
    return BASE_WEIGHT + math.ceil(max(0, n_items) / 20)


class WeightPacer:
    """Token bucket over request weight: at most ``weight_per_minute`` per rolling minute (burst = a quarter of it),
    plus a hard wall-clock ``deadline`` for the whole call. ``spend`` blocks (``sleep``) until the bucket allows."""

    def __init__(self, weight_per_minute: int = 600, *, max_seconds: float = 240.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        if weight_per_minute <= 0:
            raise ValueError("weight_per_minute must be > 0")
        self.rate = weight_per_minute / 60.0
        self.capacity = max(float(BASE_WEIGHT) * 2, weight_per_minute / 4.0)
        self.tokens = self.capacity
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self.deadline = self._last + max_seconds
        self.spent = 0
        self.requests = 0

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def time_left(self) -> float:
        return self.deadline - self._clock()

    def can_start(self, weight: int = BASE_WEIGHT) -> bool:
        """False when waiting for ``weight`` would overrun the deadline (the caller stops; cursors resume later)."""
        self._refill()
        wait = max(0.0, (weight - self.tokens) / self.rate)
        return self._clock() + wait < self.deadline

    def spend(self, weight: int = BASE_WEIGHT) -> None:
        self._refill()
        if self.tokens < weight:
            self._sleep((weight - self.tokens) / self.rate)
            self._refill()
        self.tokens -= weight
        self.spent += weight
        self.requests += 1

    def settle(self, actual_weight: int, estimated_weight: int) -> None:
        """Charge the difference once the response size is known."""
        extra = actual_weight - estimated_weight
        if extra:
            self.tokens -= extra
            self.spent += extra


def make_info_client(settings: Any = None, *, info: Optional[Any] = None) -> Any:
    """``info`` if injected (tests / callers with their own client), else ``app.hl.info.InfoClient`` on
    ``settings.hl_api_url`` (read-only; never /exchange)."""
    if info is not None:
        return info
    from app.hl.info import InfoClient

    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return InfoClient(settings.hl_api_url, timeout=15.0, max_retries=3)
