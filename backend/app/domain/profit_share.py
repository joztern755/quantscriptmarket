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
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import Economics
from app.errors import ValidationFailed
from app.money import BPS

from ._common import require_int, require_non_negative

__all__ = [
    "MODES",
    "SubscriptionPnlState",
    "SettlementResult",
    "validate_creator_bps",
    "user_rate_bps",
    "settle",
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
