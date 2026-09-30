"""Fee math (SPEC §1): builder fee and its split, subscription split, paid-post split, plan pricing.

Rounding contract (SPEC §1): every fee charged to a user is floored to the micro; every split allocates its
rounding remainder to the platform, so the parts of a split always sum exactly to the input.
"""
from __future__ import annotations

from app.config import Economics
from app.errors import ValidationFailed
from app.money import BPS, split_exact

from ._common import require_int, require_non_negative

__all__ = [
    "TENTHS_BP_DENOMINATOR",
    "PERP_MAX_BUILDER_FEE_TENTHS_BP",
    "builder_fee_micro",
    "split_builder_fee",
    "subscription_split",
    "validate_post_price",
    "post_sale_split",
    "plan_price",
    "plan_allows_active_strategies",
]

#: Hyperliquid's builder `f` is in tenths of a basis point: fee = notional × f / 100_000.
TENTHS_BP_DENOMINATOR = 100_000
#: Hyperliquid perp builder-fee maximum (0.1%).
PERP_MAX_BUILDER_FEE_TENTHS_BP = 100


def _econ(economics: Economics | None) -> Economics:
    return economics if economics is not None else Economics()


def builder_fee_micro(notional_micro: int, fee_tenths_bp: int) -> int:
    """Builder fee for one fill: |notional| × f / 100_000, floored (fees charged to users round down).

    `notional_micro` may be signed (sell side); the fee is always computed on the absolute notional.
    `fee_tenths_bp` must be in [0, 100] (Hyperliquid perp max 0.1%).
    """
    n = abs(require_int("notional_micro", notional_micro))
    f = require_non_negative("fee_tenths_bp", fee_tenths_bp)
    if f > PERP_MAX_BUILDER_FEE_TENTHS_BP:
        raise ValidationFailed("builder fee above perp maximum", fee_tenths_bp=f)
    return (n * f) // TENTHS_BP_DENOMINATOR


def split_builder_fee(
    fee_micro: int,
    in_house: bool,
    referrer_share_of_pool_bps: int | None,
    economics: Economics | None = None,
) -> dict[str, int]:
    """Split a builder fee actually charged into {"creator", "platform", "referrer"} (exact sum).

    1. fee → creator / platform / referral pool by `builder_split_*_bps` (default 50/30/20), each floored,
       remainder → platform.
    2. in-house strategy: the creator part → platform (creator = 0).
    3. no referrer (`referrer_share_of_pool_bps is None`): whole pool → platform.
       Otherwise referrer = floor(pool × share / 10_000); pool remainder → platform.
    """
    e = _econ(economics)
    fee = require_non_negative("fee_micro", fee_micro)
    parts = split_exact(
        fee,
        {
            "creator": e.builder_split_creator_bps,
            "platform": e.builder_split_platform_bps,
            "pool": e.builder_split_referral_pool_bps,
        },
        remainder_to="platform",
    )
    creator, platform, pool = parts["creator"], parts["platform"], parts["pool"]
    if in_house:
        platform += creator
        creator = 0
    referrer = 0
    if referrer_share_of_pool_bps is not None:
        share = require_non_negative("referrer_share_of_pool_bps", referrer_share_of_pool_bps)
        if share > BPS:
            raise ValidationFailed("referrer share above 100% of pool", share_bps=share)
        referrer = (pool * share) // BPS
    platform += pool - referrer
    out = {"creator": creator, "platform": platform, "referrer": referrer}
    assert sum(out.values()) == fee  # invariant: exact split
    return out


def subscription_split(price_micro: int, economics: Economics | None = None) -> tuple[int, int]:
    """Monthly strategy subscription → (creator, platform). Platform keeps `subscription_platform_bps` (3%).

    creator = floor(price × (10_000 − 300) / 10_000); platform = price − creator (the remainder goes to the
    platform, per the split rule). A price of 0 (free strategy) returns (0, 0).
    """
    e = _econ(economics)
    price = require_non_negative("price_micro", price_micro)
    creator = (price * (BPS - e.subscription_platform_bps)) // BPS
    return creator, price - creator


def validate_post_price(price_micro: int, economics: Economics | None = None) -> int:
    """A creator-set post price is valid if it is 0 (free post) or ≥ `post_min_price_micro` ($2)."""
    e = _econ(economics)
    price = require_non_negative("price_micro", price_micro)
    if price != 0 and price < e.post_min_price_micro:
        raise ValidationFailed("post price below minimum", price_micro=price, min_micro=e.post_min_price_micro)
    return price


def post_sale_split(price_micro: int, economics: Economics | None = None) -> tuple[int, int]:
    """Paid-post sale → (creator, platform). Platform keeps a flat `post_platform_fee_micro` ($1).

    A sale requires price ≥ `post_min_price_micro` ($2); free posts (0) are not sales and are rejected here.
    """
    e = _econ(economics)
    price = require_non_negative("price_micro", price_micro)
    if price < e.post_min_price_micro:
        raise ValidationFailed("post price below minimum", price_micro=price, min_micro=e.post_min_price_micro)
    platform = min(e.post_platform_fee_micro, price)
    return price - platform, platform


def plan_price(plan_key: str, economics: Economics | None = None) -> int:
    """Monthly price (micro) of a platform plan (free/pro/max). Unknown plan → ValidationFailed."""
    e = _econ(economics)
    try:
        return e.plan(plan_key).price_monthly_micro
    except KeyError:
        raise ValidationFailed("unknown plan", plan=plan_key) from None


def plan_allows_active_strategies(plan_key: str, active_count: int, economics: Economics | None = None) -> bool:
    """True if a user on `plan_key` may have `active_count` active strategy subscriptions."""
    e = _econ(economics)
    n = require_non_negative("active_count", active_count)
    try:
        limit = e.plan(plan_key).max_active_strategies
    except KeyError:
        raise ValidationFailed("unknown plan", plan=plan_key) from None
    return limit is None or n <= limit
