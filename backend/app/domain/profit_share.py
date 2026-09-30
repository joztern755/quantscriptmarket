"""Profit-share settlement above a high-water mark (SPEC §1, §1.1).

Per subscription, each daily settlement:
    cum_pnl += attributed PnL delta since the last settlement (may be negative)
    profit   = max(0, cum_pnl − hwm)
    if profit > 0: charge profit × rate (floored) and set hwm = cum_pnl
Losses never refund and must be recovered (cum_pnl back above hwm) before new profit share is charged.

Platform modes (`Economics.platform_profit_share_mode`):
- ``on_top``:     user pays creator_bps + platform_bps; creator gets creator_bps, platform the rest.
- ``carved_out``: user pays creator_bps; the platform takes platform_bps out of it. If creator_bps <
                  platform_bps the platform takes all of creator_bps (creator gets 0).
In-house strategies: the creator part goes to the platform (`in_house=True`).

`settle` is pure and deterministic; idempotency on (subscription_id, settle_date) is the caller's job.

HWM on a zero charge (REVIEW_MONEY L3, kept on purpose): the HWM advances whenever there was profit, even when
``floor(profit × rate)`` rounds to 0 (profit below ~8 micro-USD a day at 13.5 %). Not advancing it would let the
same micro-profit be charged later; advancing it forgoes at most a few micro-USD per subscription per day.

Collection (REVIEW_MONEY C1): profit share is post-paid and may exceed the user's fee balance. ``split_collected``
splits a charge into the part the user's NON-NEGATIVE balance covers (credited to the creator payable / platform
revenue) and the uncollected rest (credited to the ``ps_pending:{user}:…`` accounts, never payable).
``release_pro_rata`` is the Python twin of SQL ``ps_pending_release`` (0010_money_fixes.sql): after a top-up the pending
total is cut down to the user's remaining debt, pro-rata per pending account.

Position book (REVIEW_MONEY H1): PnL is attributed from a per-subscription book built from OUR fills only (average
entry cost), never from Hyperliquid's account-level ``closedPnl``. ``PositionBook`` documents the policy for fills that
are not ours (manual trades on a strategy coin), for pause and for cancel-"leave": the book is marked to market and
the unrealised gain/loss is attributed (and profit share charged on it above the HWM like any other PnL).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Mapping

from app.config import Economics
from app.errors import ValidationFailed
from app.money import BPS, MICRO

from ._common import require_int, require_non_negative

__all__ = [
    "MODES",
    "SubscriptionPnlState",
    "SettlementResult",
    "validate_creator_bps",
    "user_rate_bps",
    "settle",
    "CollectedSplit",
    "split_collected",
    "release_pro_rata",
    "PositionBook",
    "BookStep",
]

MODES = ("on_top", "carved_out")


@dataclass(frozen=True)
class SubscriptionPnlState:
    cum_pnl_micro: int = 0
    hwm_micro: int = 0  # starts at 0 at subscription start


@dataclass(frozen=True)
class SettlementResult:
    profit: int          # micro; profit above HWM this settlement (0 if none)
    charge_total: int    # micro; total debited from the user's fee balance (= creator_part + platform_part)
    creator_part: int    # micro; → creator payable
    platform_part: int   # micro; → platform:revenue:profit_share
    new_state: SubscriptionPnlState


def _econ(economics: Economics | None) -> Economics:
    return economics if economics is not None else Economics()


def validate_creator_bps(creator_bps: int, economics: Economics | None = None) -> int:
    """Creator profit share must be in [0, profit_share_creator_cap_bps] (default 0–1500)."""
    e = _econ(economics)
    bps = require_int("creator_bps", creator_bps)
    if bps < 0 or bps > e.profit_share_creator_cap_bps:
        raise ValidationFailed("creator profit share out of range", creator_bps=bps, cap_bps=e.profit_share_creator_cap_bps)
    return bps


def _mode(e: Economics) -> str:
    if e.platform_profit_share_mode not in MODES:
        raise ValidationFailed("unknown platform profit share mode", mode=e.platform_profit_share_mode)
    return e.platform_profit_share_mode


def user_rate_bps(creator_bps: int, economics: Economics | None = None) -> int:
    """Total profit-share rate the user pays (for the subscribe-gate fee summary)."""
    e = _econ(economics)
    c = validate_creator_bps(creator_bps, e)
    return c + e.platform_profit_share_bps if _mode(e) == "on_top" else c


def settle(
    state: SubscriptionPnlState,
    new_pnl_delta_micro: int,
    creator_bps: int,
    economics: Economics | None = None,
    *,
    in_house: bool = False,
) -> SettlementResult:
    """Apply one settlement period's attributed PnL delta and compute the profit-share charge (see module doc)."""
    e = _econ(economics)
    c = validate_creator_bps(creator_bps, e)
    mode = _mode(e)
    p_bps = require_non_negative("platform_profit_share_bps", e.platform_profit_share_bps)
    cum = require_int("cum_pnl_micro", state.cum_pnl_micro) + require_int("new_pnl_delta_micro", new_pnl_delta_micro)
    hwm = require_int("hwm_micro", state.hwm_micro)

    profit = max(0, cum - hwm)
    if profit == 0:
        return SettlementResult(0, 0, 0, 0, SubscriptionPnlState(cum, hwm))

    if mode == "on_top":
        total_bps, creator_bps_eff = c + p_bps, c
    else:  # carved_out
        total_bps, creator_bps_eff = c, c - min(p_bps, c)

    charge = (profit * total_bps) // BPS              # floor: fees charged to users round down
    creator = (profit * creator_bps_eff) // BPS        # floor; remainder of the charge → platform
    platform = charge - creator
    if in_house:
        platform, creator = platform + creator, 0
    # HWM advances whenever there was profit, even if the rate (or rounding) made the charge 0.
    return SettlementResult(profit, charge, creator, platform, SubscriptionPnlState(cum, cum))


# ------------------------------------------------------------------------------------------------ collection (C1)

@dataclass(frozen=True)
class CollectedSplit:
    creator_collected: int     # → creator:{id}:payable
    platform_collected: int    # → platform:revenue:profit_share
    creator_pending: int       # → ps_pending:{user}:{creator}
    platform_pending: int      # → ps_pending:{user}:platform

    @property
    def collected(self) -> int:
        return self.creator_collected + self.platform_collected

    @property
    def pending(self) -> int:
        return self.creator_pending + self.platform_pending


def split_collected(creator_micro: int, platform_micro: int, spendable_micro: int) -> CollectedSplit:
    """Split a profit-share charge (creator + platform parts) by what the user's fee balance covers.

    collected = min(charge, max(0, spendable)); creator_collected = floor(creator × collected / charge), the platform
    gets the rest of the collected part (= ceil(platform × collected / charge) ≤ platform). Exact: the four parts sum
    to the charge, the collected parts to ``collected``."""
    c = require_non_negative("creator_micro", creator_micro)
    p = require_non_negative("platform_micro", platform_micro)
    spend = require_int("spendable_micro", spendable_micro)
    total = c + p
    if total == 0:
        return CollectedSplit(0, 0, 0, 0)
    collected = min(total, max(0, spend))
    cc = (c * collected) // total
    pc = collected - cc
    return CollectedSplit(cc, pc, c - cc, p - pc)


def release_pro_rata(pending: Mapping[str, int], debt_micro: int) -> dict[str, int]:
    """Twin of SQL ``ps_pending_release``: amounts to release per pending account so that what stays pending is
    ``min(Σ pending, debt)``. Floor pro-rata shares; the remainder goes to accounts in code order (never above an
    account's balance). Accounts with nothing to release are omitted."""
    q = {k: int(v) for k, v in pending.items() if int(v) > 0}
    total = sum(q.values())
    release = total - min(total, max(0, int(debt_micro)))
    if release <= 0:
        return {}
    alloc = {k: (release * v) // total for k, v in q.items()}
    left = release - sum(alloc.values())
    for k in sorted(q):
        if left <= 0:
            break
        extra = min(left, q[k] - alloc[k])
        alloc[k] += extra
        left -= extra
    return {k: v for k, v in alloc.items() if v > 0}


# ------------------------------------------------------------------------------------------------ position book (H1)

ZERO = Decimal(0)
_AVG_QUANT = Decimal("1e-18")


def _sign(d: Decimal) -> int:
    return (d > 0) - (d < 0)


def _floor_micro(usd_amount: Decimal) -> int:
    return int((usd_amount * MICRO).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class BookStep:
    book: "PositionBook"
    pnl_micro: int = 0                 # attributed PnL of this step (floored toward −∞: never overstated)
    closed_qty: Decimal = ZERO         # quantity closed against the book's average entry
    excess_qty: Decimal = ZERO         # reduce-only quantity beyond the book (it closed someone else's exposure)


@dataclass(frozen=True)
class PositionBook:
    """One subscription's position in one coin as built from OUR fills (average-cost method).

    Policy (documented for REVIEW_MONEY H1):
    - ``own_fill``: every fill of an order we recorded for the subscription. Realised PnL of the closed part =
      closed × (px − avg entry) × side, minus the fill's fee (fee incl. builder fee). A reduce-only fill larger than
      the book closes the book and the excess is ignored (it closed exposure the user added by hand). A non
      reduce-only fill crossing zero flips the book at the fill price.
    - ``foreign_fill``: a fill on a strategy coin in the trading account that is NOT one of our orders (manual trade,
      another app, a forged platform-prefixed cloid). The whole book is marked to market at the fill price (gain/loss
      attributed), and if the fill reduces the book the closed quantity leaves the book (the user took it over at that
      price). A fill adding exposure changes nothing (not ours).
    - ``mark_to_market`` (pause, cancel "leave"): the unrealised PnL at the mark price is attributed and the average
      entry steps to the mark, so a later close is only charged on the move after the mark (no double charge).
    Quantities are signed Decimals (+ long / − short)."""
    qty: Decimal = ZERO
    avg_px: Decimal = ZERO

    def unrealized_micro(self, px: Decimal) -> int:
        return _floor_micro(self.qty * (Decimal(px) - self.avg_px)) if self.qty else 0

    def own_fill(self, signed_sz: Decimal, px: Decimal, fee: Decimal = ZERO, *, reduce_only: bool = False) -> BookStep:
        s, px, fee = Decimal(signed_sz), Decimal(px), Decimal(fee)
        if s == 0:
            return BookStep(self, _floor_micro(-fee))
        q, avg = self.qty, self.avg_px
        if q == 0 or _sign(q) == _sign(s):
            if reduce_only:          # a reduce-only fill never opens/increases the book (it reduced foreign exposure)
                return BookStep(self, _floor_micro(-fee), excess_qty=abs(s))
            new_q = q + s
            new_avg = ((abs(q) * avg + abs(s) * px) / abs(new_q)).quantize(_AVG_QUANT)
            return BookStep(PositionBook(new_q, new_avg), _floor_micro(-fee))
        closed = min(abs(s), abs(q))
        gross = closed * (px - avg) * _sign(q)
        rest = abs(s) - closed
        if rest == 0:
            new_q = q + s
            return BookStep(PositionBook(new_q, avg if new_q != 0 else ZERO), _floor_micro(gross - fee), closed)
        if reduce_only:
            return BookStep(PositionBook(ZERO, ZERO), _floor_micro(gross - fee), closed, excess_qty=rest)
        return BookStep(PositionBook(rest * _sign(s), px), _floor_micro(gross - fee), closed)

    def mark_to_market(self, px: Decimal) -> BookStep:
        px = Decimal(px)
        if self.qty == 0:
            return BookStep(PositionBook(ZERO, ZERO))
        return BookStep(PositionBook(self.qty, px), _floor_micro(self.qty * (px - self.avg_px)))

    def foreign_fill(self, signed_sz: Decimal, px: Decimal) -> BookStep:
        s = Decimal(signed_sz)
        if self.qty == 0 or s == 0 or _sign(s) == _sign(self.qty):
            return BookStep(self)                                   # adds exposure: not ours, nothing attributed
        marked = self.mark_to_market(px)
        closed = min(abs(s), abs(self.qty))
        new_q = self.qty + closed * _sign(s)
        return BookStep(PositionBook(new_q, Decimal(px) if new_q != 0 else ZERO), marked.pnl_micro, closed)
