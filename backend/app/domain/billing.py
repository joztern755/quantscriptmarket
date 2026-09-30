"""Fee-balance billing (SPEC §1 "Fee balance" / "Insufficient balance"): subscription status machine,
monthly renewal schedule with month-end clamping, low-balance alert thresholds.

Status machine (`next_status`), evaluated whenever something is due (renewal, daily profit-share settlement)
or the balance changes (top-up):

    active      --insufficient-->            past_due      (past_due_since = now)
    past_due    --insufficient, < grace-->   past_due      (still trades normally: grace period)
    past_due    --insufficient, ≥ grace-->   reduce_only   (no new entries; exits allowed)
    reduce_only --insufficient-->            reduce_only
    active|past_due|reduce_only --balance covers amount due--> active (past_due_since cleared; caller deducts)
    paused_user, cancelled, pending          unchanged (billing never overrides a user pause, a cancellation
                                             or an activation still in progress)

"Insufficient" means balance < amount_due or balance < 0.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Iterable, Sequence

from app.money import BPS

from ._common import require_aware, require_int, require_non_negative

__all__ = [
    "ACTIVE", "PAST_DUE", "REDUCE_ONLY", "PAUSED_USER", "CANCELLED", "PENDING",
    "STATUSES",
    "StatusDecision",
    "next_status",
    "entries_allowed",
    "exits_allowed",
    "add_months",
    "renewal_at",
    "renewal_schedule",
    "next_renewal_after",
    "LOW_BALANCE_THRESHOLDS_BPS",
    "estimate_monthly_need",
    "crossed_low_balance_thresholds",
]

ACTIVE, PAST_DUE, REDUCE_ONLY, PAUSED_USER, CANCELLED, PENDING = (
    "active", "past_due", "reduce_only", "paused_user", "cancelled", "pending",
)
STATUSES = (ACTIVE, PAST_DUE, REDUCE_ONLY, PAUSED_USER, CANCELLED, PENDING)
_BILLABLE = (ACTIVE, PAST_DUE, REDUCE_ONLY)


@dataclass(frozen=True)
class StatusDecision:
    status: str
    past_due_since: datetime | None
    changed: bool
    reason: str   # "paid" | "insufficient_balance" | "grace_expired" | "past_due_since_missing" | "unchanged"


def next_status(
    current: str,
    balance_micro: int,
    amount_due_micro: int,
    past_due_since: datetime | None,
    now: datetime,
    grace_hours: int = 72,
) -> StatusDecision:
    """Next billing status for a subscription (see module docstring for the state table).

    `amount_due_micro` is what must be paid now (renewal price, or 0 when only re-checking after a top-up).
    When the result is `active` and amount_due > 0 the caller deducts it from the fee balance.
    Fail closed: a `past_due` row with no `past_due_since` is treated as past grace → `reduce_only`.
    """
    if current not in STATUSES:
        raise ValueError(f"unknown subscription status {current!r}")
    bal = require_int("balance_micro", balance_micro)
    due = require_non_negative("amount_due_micro", amount_due_micro)
    now = require_aware("now", now)
    grace = require_non_negative("grace_hours", grace_hours)
    since = require_aware("past_due_since", past_due_since) if past_due_since is not None else None

    if current not in _BILLABLE:
        return StatusDecision(current, since, False, "unchanged")

    if bal >= 0 and bal >= due:
        return StatusDecision(ACTIVE, None, current != ACTIVE, "paid" if current != ACTIVE else "unchanged")

    if current == ACTIVE:
        return StatusDecision(PAST_DUE, now, True, "insufficient_balance")
    if current == PAST_DUE:
        if since is None:
            return StatusDecision(REDUCE_ONLY, now, True, "past_due_since_missing")
        if now - since >= timedelta(hours=grace):
            return StatusDecision(REDUCE_ONLY, since, True, "grace_expired")
        return StatusDecision(PAST_DUE, since, False, "unchanged")
    return StatusDecision(REDUCE_ONLY, since, False, "unchanged")


def entries_allowed(status: str, past_due_since: datetime | None, now: datetime, grace_hours: int = 72) -> bool:
    """May the subscription open/increase positions? Only `active`, or `past_due` still inside the grace
    period. Fail closed on unknown statuses and on `past_due` without a timestamp."""
    if status == ACTIVE:
        return True
    if status == PAST_DUE and past_due_since is not None:
        now = require_aware("now", now)
        since = require_aware("past_due_since", past_due_since)
        return now - since < timedelta(hours=require_non_negative("grace_hours", grace_hours))
    return False


def exits_allowed(status: str) -> bool:
    """May the strategy reduce/close positions? `active`, `past_due`, `reduce_only`. A user pause, a
    cancellation or a pending activation means the executor places no orders at all."""
    return status in (ACTIVE, PAST_DUE, REDUCE_ONLY)


# ---------------------------------------------------------------- renewal schedule

def add_months(dt: datetime, months: int) -> datetime:
    """Same wall-clock time `months` later; day clamped to the month's last day (Jan 31 + 1 → Feb 28/29)."""
    require_int("months", months)
    idx = dt.year * 12 + (dt.month - 1) + months
    year, month0 = divmod(idx, 12)
    if not 1 <= year <= 9999:
        raise ValueError("date out of range")
    last = calendar.monthrange(year, month0 + 1)[1]
    return dt.replace(year=year, month=month0 + 1, day=min(dt.day, last))


def renewal_at(anchor: datetime, period_index: int) -> datetime:
    """Start of billing period `period_index` (0 = the subscription start). Always computed from the original
    anchor so clamping does not drift: Jan 31 → Feb 28 → Mar 31 → Apr 30."""
    require_aware("anchor", anchor)
    if require_int("period_index", period_index) < 0:
        raise ValueError("period_index must be >= 0")
    return add_months(anchor, period_index)


def renewal_schedule(anchor: datetime, count: int, start_index: int = 1) -> list[datetime]:
    """`count` successive renewal instants starting at period `start_index` (default: the first renewal)."""
    require_non_negative("count", count)
    return [renewal_at(anchor, start_index + i) for i in range(count)]


def next_renewal_after(anchor: datetime, now: datetime) -> datetime:
    """First renewal (period index ≥ 1) strictly after `now`. Equals `current_period_end`."""
    a = require_aware("anchor", anchor)
    n = require_aware("now", now)
    k = max(1, (n.year - a.year) * 12 + (n.month - a.month))
    # step back in case we overshot, then forward to the first instant strictly after now
    while k > 1 and renewal_at(anchor, k - 1) > n:
        k -= 1
    while renewal_at(anchor, k) <= n:
        k += 1
    return renewal_at(anchor, k)


# ---------------------------------------------------------------- low-balance alerts

LOW_BALANCE_THRESHOLDS_BPS: tuple[int, ...] = (5000, 2000, 0)   # 50%, 20%, 0% of estimated monthly need


def estimate_monthly_need(
    subscription_prices_micro: Iterable[int] = (),
    plan_price_micro: int = 0,
    expected_profit_share_micro: int = 0,
) -> int:
    """Estimated monthly fee-balance need: Σ subscription prices + plan price + expected profit share."""
    total = sum(require_non_negative("subscription_price_micro", p) for p in subscription_prices_micro)
    return total + require_non_negative("plan_price_micro", plan_price_micro) + require_non_negative(
        "expected_profit_share_micro", expected_profit_share_micro
    )


def crossed_low_balance_thresholds(
    prev_balance_micro: int | None,
    new_balance_micro: int,
    monthly_need_micro: int,
    thresholds_bps: Sequence[int] = LOW_BALANCE_THRESHOLDS_BPS,
) -> tuple[int, ...]:
    """Thresholds (bps of monthly need, descending) newly crossed by a balance change.

    A threshold t has level = floor(need × t / 10_000) and is crossed when prev > level ≥ new (the 0% threshold
    fires when the balance reaches ≤ 0). A later top-up above the level re-arms it naturally. `prev=None`
    (first observation) counts as +∞. No alerts when the need is ≤ 0.
    """
    new = require_int("new_balance_micro", new_balance_micro)
    need = require_int("monthly_need_micro", monthly_need_micro)
    prev = None if prev_balance_micro is None else require_int("prev_balance_micro", prev_balance_micro)
    if need <= 0:
        return ()
    out = []
    for t in sorted({require_non_negative("threshold_bps", t) for t in thresholds_bps}, reverse=True):
        level = (need * t) // BPS
        if new <= level and (prev is None or prev > level):
            out.append(t)
    return tuple(out)
