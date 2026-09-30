"""Hyperliquid read access for the data jobs: the ``/info`` client factory and a request-weight pacer.

Rate limits (Hyperliquid docs, recalled — UNVERIFIED from this environment, SPEC §6; all weights are configuration in
``app.config.HlLimits``): 1200 weight / minute / IP for ``/info``; most requests weigh 20; ``candleSnapshot`` adds 1
per 60 candles returned; ``userFills*`` / ``userFunding`` add 1 per 20 items. Two layers of pacing:

* per job (``WeightPacer`` token bucket, ``weight_per_minute``, default 600) plus a wall-clock deadline;
* SHARED across every job, instance and the executor tick on the same egress IP (``app.hl.budget.HlRateBudget``,
  table ``hl_rate_budget``; default 800/min with 300/min reserved for the tick). ``can_start`` / ``spend`` charge the
  JOBS pool; when it is spent the pacer waits for the next minute window, and when that would overrun the job's
  deadline ``can_start`` returns False (the job stops; its cursors resume next call) or ``spend`` raises
  ``HlBudgetExhausted`` (an ``AppError``: the jobs treat it like a failed request for that unit).

The shared budget is used whenever the job talks to the REAL Hyperliquid (no injected ``info``) and
``settings.hl_limits.shared_budget`` is on; tests inject it explicitly (``rate_budget=``).
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

__all__ = ["WeightPacer", "make_info_client", "make_pacer", "shared_budget_for", "candle_weight", "list_weight",
           "BASE_WEIGHT"]

BASE_WEIGHT = 20


def candle_weight(n_items: int) -> int:
    return BASE_WEIGHT + math.ceil(max(0, n_items) / 60)


def list_weight(n_items: int) -> int:
    return BASE_WEIGHT + math.ceil(max(0, n_items) / 20)


class WeightPacer:
    """Token bucket over request weight: at most ``weight_per_minute`` per rolling minute (burst = a quarter of it),
    plus a hard wall-clock ``deadline`` for the whole call. ``spend`` blocks (``sleep``) until the bucket allows."""

    def __init__(self, weight_per_minute: int = 600, *, max_seconds: float = 240.0,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 shared: Any = None, pool: str = "jobs") -> None:
        if weight_per_minute <= 0:
            raise ValueError("weight_per_minute must be > 0")
        self.shared = shared            # app.hl.budget.HlRateBudget (or None: local pacing only)
        self.pool = pool
        self._prepaid = 0               # shared weight already acquired by can_start, consumed by spend
        self.budget_waits = 0
        self.budget_refused = False
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
        """False when waiting for ``weight`` would overrun the deadline (the caller stops; cursors resume later).
        With a shared budget this also ACQUIRES ``weight`` from it (waiting for later minute windows while the
        deadline allows); the following ``spend(weight)`` consumes that pre-paid weight."""
        self._refill()
        wait = max(0.0, (weight - self.tokens) / self.rate)
        if self._clock() + wait >= self.deadline:
            return False
        if self.shared is None or self._prepaid >= weight:
            return True
        need = weight - self._prepaid
        if not self._acquire_shared(need):
            return False
        self._prepaid += need
        return True

    def spend(self, weight: int = BASE_WEIGHT) -> None:
        if self.shared is not None:
            if self._prepaid >= weight:
                self._prepaid -= weight
            else:
                need, self._prepaid = weight - self._prepaid, 0
                if not self._acquire_shared(need):
                    from app.hl.budget import HlBudgetExhausted

                    raise HlBudgetExhausted("hyperliquid rate budget exhausted for this run", pool=self.pool)
        self._refill()
        if self.tokens < weight:
            self._sleep((weight - self.tokens) / self.rate)
            self._refill()
        self.tokens -= weight
        self.spent += weight
        self.requests += 1

    def settle(self, actual_weight: int, estimated_weight: int) -> None:
        """Charge the difference once the response size is known (the shared budget only ever gets charged MORE:
        an over-estimate is not refunded there — conservative)."""
        extra = actual_weight - estimated_weight
        if extra:
            self.tokens -= extra
            self.spent += extra
        if extra > 0 and self.shared is not None:
            self.shared.try_acquire(extra, self.pool, force=True)

    def _acquire_shared(self, weight: int) -> bool:
        ok = self.shared.acquire_wait(weight, self.pool, deadline=self.deadline, monotonic=self._clock,
                                      sleep=self._counting_sleep)
        if not ok:
            self.budget_refused = True
        return ok

    def _counting_sleep(self, seconds: float) -> None:
        self.budget_waits += 1
        self._sleep(seconds)


def shared_budget_for(db: Any, settings: Any = None) -> Any:
    """The shared ``HlRateBudget`` for ``db`` per ``settings.hl_limits`` (None when disabled)."""
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    limits = getattr(settings, "hl_limits", None)
    if limits is None or not getattr(limits, "shared_budget", False):
        return None
    from app.hl.budget import HlRateBudget

    return HlRateBudget(db, limits)


def make_pacer(db: Any, settings: Any = None, *, info: Optional[Any] = None, weight_per_minute: int = 600,
               max_seconds: float = 240.0, rate_budget: Any = None) -> WeightPacer:
    """The job's pacer: local bucket + deadline, plus the shared per-IP budget when talking to the real Hyperliquid
    (``info`` not injected) or when ``rate_budget`` is given explicitly."""
    shared = rate_budget if rate_budget is not None else (shared_budget_for(db, settings) if info is None else None)
    return WeightPacer(weight_per_minute, max_seconds=max_seconds, shared=shared)


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
