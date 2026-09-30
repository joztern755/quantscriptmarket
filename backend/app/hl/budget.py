"""Shared Hyperliquid ``/info`` rate budget, DB-backed (table ``hl_rate_budget``, migrations/0009_hardening.sql).

Why: Hyperliquid limits request WEIGHT per minute per IP (see ``app.config.HlLimits`` — numbers UNVERIFIED, all
configurable). The executor tick, the data jobs (candles-sync, fills-ingest, funding-scan, deposits-scan,
agent-expiry-scan) and reconcile all leave through the same egress IP, possibly from several Cloud Run instances at
once, so an in-process pacer alone cannot keep the total under the limit. Every caller charges one shared counter
per (egress key, UTC minute) in Postgres, atomically (one ``INSERT … ON CONFLICT DO UPDATE … WHERE`` statement).

Pools and priority:
* ``tick`` — the executor tick (orders, exits, creator signals). ALWAYS charged, NEVER blocked or refused: a missed
  exit is worse than a 429 (which ``InfoClient`` retries). Overruns are logged (``hl_budget_over``).
* ``jobs`` — data jobs and reconcile. Granted only while ``jobs + w <= budget − max(tick, tick_reserve)``, i.e. the
  tick always keeps its reserve and anything it uses beyond the reserve is taken away from the jobs. A refused job
  waits for the next minute window (bounded by its own deadline) — callers stop and resume from their cursors.

Storage: a ring of 60 rows per egress key (slot = minute of the hour); a slot holding an older minute is reset on
first use, so the table never grows and nothing is ever deleted. A process with a clock behind the row's window
charges the newer window (never resets it backwards).

Failure policy: if the budget table cannot be reached the call is ALLOWED (fail open, logged at most once a minute):
the budget is a courtesy limiter; Hyperliquid's own 429 + ``InfoClient`` backoff remain the hard stop.
"""
from __future__ import annotations

import contextvars
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping, Optional

from app.errors import ExternalServiceError
from app.logging import get_logger

__all__ = ["POOL_TICK", "POOL_JOBS", "HlBudgetExhausted", "HlRateBudget", "BudgetHook", "Charge", "budget_pool",
           "current_pool"]

log = get_logger("app.hl.budget")

POOL_TICK = "tick"
POOL_JOBS = "jobs"
_POOLS = (POOL_TICK, POOL_JOBS)
UTC = timezone.utc

_pool_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("hl_budget_pool", default=None)


@contextmanager
def budget_pool(pool: str) -> Iterator[None]:
    """Charge every budgeted ``/info`` call made in this block (same thread / task) to ``pool``."""
    if pool not in _POOLS:
        raise ValueError(f"unknown budget pool {pool!r}")
    token = _pool_var.set(pool)
    try:
        yield
    finally:
        _pool_var.reset(token)


def current_pool(default: str = POOL_TICK) -> str:
    return _pool_var.get() or default


class HlBudgetExhausted(ExternalServiceError):
    """The shared Hyperliquid budget had no room for this caller before its deadline (a job stops and resumes)."""


@dataclass(frozen=True)
class Charge:
    granted: bool
    window_start: Optional[datetime] = None
    spent_tick: int = 0
    spent_jobs: int = 0
    over: bool = False            # tick charged beyond the whole budget (forced)
    fail_open: bool = False       # budget unavailable → allowed without accounting


_SQL = """
INSERT INTO hl_rate_budget AS b (egress_key, slot, window_start, spent_tick, spent_jobs)
SELECT CAST(:e AS text), CAST(:slot AS smallint), CAST(:w AS timestamptz), CAST(:wt AS integer), CAST(:wj AS integer)
 WHERE CAST(:first_ok AS boolean)
ON CONFLICT (egress_key, slot) DO UPDATE SET
       window_start = GREATEST(b.window_start, EXCLUDED.window_start),
       spent_tick = (CASE WHEN b.window_start >= EXCLUDED.window_start THEN b.spent_tick ELSE 0 END) + EXCLUDED.spent_tick,
       spent_jobs = (CASE WHEN b.window_start >= EXCLUDED.window_start THEN b.spent_jobs ELSE 0 END) + EXCLUDED.spent_jobs,
       updated_at = now()
 WHERE CAST(:force AS boolean)
    OR (CAST(:pool AS text) = 'tick'
        AND (CASE WHEN b.window_start >= EXCLUDED.window_start THEN b.spent_tick + b.spent_jobs ELSE 0 END)
            + EXCLUDED.spent_tick <= CAST(:total AS integer))
    OR (CAST(:pool AS text) = 'jobs'
        AND (CASE WHEN b.window_start >= EXCLUDED.window_start THEN b.spent_jobs ELSE 0 END) + EXCLUDED.spent_jobs
            <= CAST(:total AS integer) - GREATEST(CASE WHEN b.window_start >= EXCLUDED.window_start
                                                       THEN b.spent_tick ELSE 0 END, CAST(:reserve AS integer)))
RETURNING window_start, spent_tick, spent_jobs"""


class HlRateBudget:
    """The shared counter. ``db``: anything ``app.execution.pg.PgDatabase`` accepts (a ``DatabasePort`` or a
    ``SqlRunner``); every charge runs in its OWN short transaction (never joins the caller's)."""

    def __init__(self, db: Any, limits: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        from app.execution.pg import PgDatabase

        self.db = PgDatabase(db)
        self.limits = limits
        self.clock = clock or (lambda: datetime.now(UTC))
        self._warned_at = 0.0
        self._lock = threading.Lock()

    # ---- window helpers ----------------------------------------------------------------------------------
    def window(self, now: datetime | None = None) -> datetime:
        n = (now or self.clock()).astimezone(UTC)
        return n.replace(second=0, microsecond=0)

    def seconds_to_next_window(self) -> float:
        now = self.clock().astimezone(UTC)
        nxt = self.window(now) + timedelta(minutes=1)
        return max(0.0, (nxt - now).total_seconds())

    # ---- charging ----------------------------------------------------------------------------------------
    def try_acquire(self, weight: int, pool: str = POOL_JOBS, *, force: bool = False) -> Charge:
        """Charge ``weight`` to ``pool`` for the current minute if it fits (``force``: charge regardless)."""
        if pool not in _POOLS:
            raise ValueError(f"unknown budget pool {pool!r}")
        lim = self.limits
        total, reserve = int(lim.budget_weight_per_minute), int(lim.tick_reserve_per_minute)
        w = max(0, int(weight))
        if pool == POOL_JOBS:
            w = min(w, total - reserve)          # one oversized request must still be grantable in an empty window
        if w == 0:
            return Charge(granted=True)
        win = self.window()
        first_ok = force or (w <= total if pool == POOL_TICK else w <= total - reserve)
        try:
            with self.db.session() as runner:
                rows = runner.fetchall(_SQL, {
                    "e": lim.egress_key, "slot": win.minute, "w": win.isoformat(),
                    "wt": w if pool == POOL_TICK else 0, "wj": w if pool == POOL_JOBS else 0,
                    "first_ok": bool(first_ok), "force": bool(force), "pool": pool, "total": total, "reserve": reserve})
        except Exception as e:  # noqa: BLE001 - fail open (module doc)
            self._warn("hl_budget_unavailable", error=type(e).__name__)
            return Charge(granted=True, fail_open=True)
        if not rows:
            return Charge(granted=False, window_start=win)
        r = dict(rows[0])
        tick, jobs = int(r["spent_tick"]), int(r["spent_jobs"])
        over = tick + jobs > total
        if over and pool == POOL_TICK:
            self._warn("hl_budget_over", spent_tick=tick, spent_jobs=jobs, budget=total)
        return Charge(granted=True, window_start=win, spent_tick=tick, spent_jobs=jobs, over=over)

    def acquire_wait(self, weight: int, pool: str = POOL_JOBS, *, deadline: float,
                     monotonic: Callable[[], float] = time.monotonic,
                     sleep: Callable[[float], None] = time.sleep) -> bool:
        """Charge ``weight``, waiting for later minute windows while ``monotonic() < deadline``. The tick pool is
        never refused (forced). Returns False when the budget had no room before the deadline."""
        if pool == POOL_TICK:
            self.try_acquire(weight, POOL_TICK, force=True)
            return True
        while True:
            if self.try_acquire(weight, pool).granted:
                return True
            wait = self.seconds_to_next_window() + 0.05 + random.random() * 0.25   # spread the herd
            if monotonic() + wait >= deadline:
                return False
            log.info("hl_budget_wait", extra={"fields": {"pool": pool, "weight": int(weight),
                                                         "wait_s": round(wait, 2)}})
            sleep(wait)

    def usage(self) -> dict[str, Any]:
        """Current minute's counters (admin console / tests)."""
        win = self.window()
        rows = self.db.all("""SELECT window_start, spent_tick, spent_jobs FROM hl_rate_budget
                               WHERE egress_key = CAST(:e AS text) AND slot = CAST(:s AS smallint)
                                 AND window_start = CAST(:w AS timestamptz)""",
                           e=self.limits.egress_key, s=win.minute, w=win.isoformat())
        tick = int(rows[0]["spent_tick"]) if rows else 0
        jobs = int(rows[0]["spent_jobs"]) if rows else 0
        return {"window_start": win.isoformat(), "spent_tick": tick, "spent_jobs": jobs, "spent": tick + jobs,
                "budget": int(self.limits.budget_weight_per_minute),
                "tick_reserve": int(self.limits.tick_reserve_per_minute)}

    def _warn(self, event: str, **fields: Any) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._warned_at < 60:
                return
            self._warned_at = now
        log.warning(event, extra={"fields": {"egress_key": self.limits.egress_key, **fields}})


class BudgetHook:
    """``InfoClient(rate_hook=…)``: ``before(body)`` charges the request's base weight before EVERY HTTP attempt
    (retries cost weight too), ``after(body, out)`` charges the per-item extra once the response size is known.
    ``budget``: an ``HlRateBudget`` or a zero-argument callable returning one (None → no accounting, e.g. before the
    executor has a database handle). Pool = ``budget_pool(...)`` context, else ``default_pool``."""

    def __init__(self, budget: Any, limits: Any, *, default_pool: str = POOL_TICK,
                 max_wait_seconds: float | None = None, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._budget = budget
        self.limits = limits
        self.default_pool = default_pool
        self.max_wait = float(max_wait_seconds if max_wait_seconds is not None else limits.max_wait_seconds)
        self._monotonic, self._sleep = monotonic, sleep

    def budget(self) -> Optional[HlRateBudget]:
        b = self._budget
        return b() if callable(b) and not isinstance(b, HlRateBudget) else b

    def before(self, body: Mapping[str, Any]) -> None:
        b = self.budget()
        if b is None:
            return
        pool = current_pool(self.default_pool)
        w = self.limits.weight(body.get("type"))
        if pool == POOL_TICK:
            b.try_acquire(w, POOL_TICK, force=True)
            return
        if not b.acquire_wait(w, pool, deadline=self._monotonic() + self.max_wait, monotonic=self._monotonic,
                              sleep=self._sleep):
            raise HlBudgetExhausted("hyperliquid rate budget exhausted", type=str(body.get("type") or ""),
                                    pool=pool)

    def after(self, body: Mapping[str, Any], out: Any) -> None:
        b = self.budget()
        if b is None:
            return
        n = len(out) if isinstance(out, list) else 0
        extra = self.limits.extra_weight(body.get("type"), n)
        if extra > 0:
            b.try_acquire(extra, current_pool(self.default_pool), force=True)
