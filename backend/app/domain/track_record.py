"""Live track record per strategy version (SPEC §3 track_record, §5.8 k-anonymity, §9 strategy detail).

Definitions:
- Only PnL events (fills' closedPnl − fee, and funding) at or after the version's `live_since` count: a new
  version resets the live record. Events after `now` are ignored. With `period_start` (leaderboard 30d/90d)
  the window is [max(live_since, period_start), now].
- total $ made = Σ pnl_micro of counted events (all subscribers, net of fees).
- time-weighted capital = Σ_span allocation × overlap(span, window) / |window| — i.e. the average capital
  deployed over the window. One `AllocationSpan` per (subscription, allocation value); an allocation change
  ends one span and starts another.
- ROI (bps) = total pnl × 10_000 / time-weighted capital, truncated toward zero; None if no capital.
- subscriber counts are DISTINCT USERS (a user with two subscriptions counts once) — this is what the
  k-anonymity threshold (default 5) is applied to.
- `not_live_proven` while the version has < 90 live days (or has never gone live).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Iterable

from app.money import BPS

from ._common import require_aware, require_int, require_non_negative

__all__ = [
    "LIVE_PROVEN_DAYS",
    "PnlEvent",
    "AllocationSpan",
    "TrackRecord",
    "PublicStats",
    "compute_track_record",
    "public_stats",
]

LIVE_PROVEN_DAYS = 90
_US = timedelta(microseconds=1)


@dataclass(frozen=True)
class PnlEvent:
    subscription_id: str
    time: datetime
    pnl_micro: int        # closedPnl − fee for a fill, or a funding payment (signed)


@dataclass(frozen=True)
class AllocationSpan:
    subscription_id: str
    user_id: str
    allocation_micro: int
    start: datetime
    end: datetime | None  # None = still open


@dataclass(frozen=True)
class TrackRecord:
    live_since: datetime | None
    window_start: datetime | None
    window_end: datetime
    live_days: int
    not_live_proven: bool
    total_pnl_micro: int
    time_weighted_capital_micro: int
    roi_bps: int | None
    subscriber_count: int           # distinct users with capital in the window
    active_subscribers: int         # distinct users with an open span at window_end
    subscription_count: int         # distinct subscriptions with capital or pnl in the window
    profitable_subscriptions: int   # subscriptions with Σ pnl > 0 in the window


@dataclass(frozen=True)
class PublicStats:
    live_since: datetime | None
    live_days: int
    not_live_proven: bool
    total_pnl_micro: int
    roi_bps: int | None
    subscriber_count: int
    active_subscribers: int
    profitable_subscriptions: int


def _overlap_us(start: datetime, end: datetime, ws: datetime, we: datetime) -> int:
    lo, hi = max(start, ws), min(end, we)
    return max(0, (hi - lo) // _US)


def compute_track_record(
    live_since: datetime | None,
    events: Iterable[PnlEvent],
    spans: Iterable[AllocationSpan],
    now: datetime,
    *,
    period_start: datetime | None = None,
    min_live_days: int = LIVE_PROVEN_DAYS,
) -> TrackRecord:
    """Aggregate a version's subscriber outcomes (see module docstring)."""
    now = require_aware("now", now)
    require_non_negative("min_live_days", min_live_days)
    if live_since is None:
        return TrackRecord(None, None, now, 0, True, 0, 0, None, 0, 0, 0, 0)
    ls = require_aware("live_since", live_since)
    ws = max(ls, require_aware("period_start", period_start)) if period_start is not None else ls
    live_days = max(0, (now - ls).days)

    per_sub: dict[str, int] = {}
    total = 0
    for ev in events:
        t = require_aware("event.time", ev.time)
        if ws <= t <= now:
            p = require_int("pnl_micro", ev.pnl_micro)
            total += p
            per_sub[ev.subscription_id] = per_sub.get(ev.subscription_id, 0) + p

    window_us = max(0, (now - ws) // _US)
    cap_acc = 0
    users: set[str] = set()
    active_users: set[str] = set()
    subs: set[str] = set(per_sub)
    for sp in spans:
        alloc = require_non_negative("allocation_micro", sp.allocation_micro)
        start = require_aware("span.start", sp.start)
        end = require_aware("span.end", sp.end) if sp.end is not None else now
        ov = _overlap_us(start, end, ws, now)
        if ov > 0 and alloc > 0:
            cap_acc += alloc * ov
            users.add(sp.user_id)
            subs.add(sp.subscription_id)
        if alloc > 0 and start <= now and (sp.end is None or require_aware("span.end", sp.end) > now):
            active_users.add(sp.user_id)

    twc = cap_acc // window_us if window_us > 0 else 0
    roi = None
    if twc > 0:
        roi = (1 if total >= 0 else -1) * ((abs(total) * BPS) // twc)
    return TrackRecord(
        live_since=ls,
        window_start=ws,
        window_end=now,
        live_days=live_days,
        not_live_proven=live_days < min_live_days,
        total_pnl_micro=total,
        time_weighted_capital_micro=twc,
        roi_bps=roi,
        subscriber_count=len(users),
        active_subscribers=len(active_users),
        subscription_count=len(subs),
        profitable_subscriptions=sum(1 for v in per_sub.values() if v > 0),
    )


def public_stats(record: TrackRecord, min_subscribers: int = 5) -> PublicStats | None:
    """Public (aggregated) stats, or None when fewer than `min_subscribers` distinct users contributed
    (k-anonymity: a lone subscriber's PnL must not be derivable)."""
    k = require_non_negative("min_subscribers", min_subscribers)
    if record.subscriber_count < max(1, k):
        return None
    return PublicStats(
        live_since=record.live_since,
        live_days=record.live_days,
        not_live_proven=record.not_live_proven,
        total_pnl_micro=record.total_pnl_micro,
        roi_bps=record.roi_bps,
        subscriber_count=record.subscriber_count,
        active_subscribers=record.active_subscribers,
        profitable_subscriptions=record.profitable_subscriptions,
    )
