"""Pre-trade risk guards and order sizing (SPEC §5.4 and §10 "Order sizing"). Pure; fails closed.

`plan_order` turns one coin's target weight for one subscription into zero, one or two IOC order plans, or a
`Rejection` with every applicable reason code.

Decisions (documented contract):
- Hard blocks — NO order at all, not even reduce-only exits: global kill switch, market kill switch, circuit
  breaker open (consecutive_rejects ≥ limit), subscription status not in {active, past_due, reduce_only}
  (paused_user / cancelled / pending), market not whitelisted, delisted, stale or future-dated data,
  mark/oracle (or mid/oracle) deviation above the cap, invalid inputs (incl. |weight| > 100x = corrupt
  signal; weights up to that are clamped by leverage, so lowering the platform cap never strands a strategy). Kill switches are emergency stops (e.g.
  suspected agent-key compromise or market manipulation), so any order is suspect; ops flatten manually.
- Entry blocks — only exposure-reducing orders: status `reduce_only`, `past_due` after the grace period
  (or without a timestamp), `new_entries_paused` (global), market in `paused_markets` (auto-pause by a
  critical alert). The target is clamped toward zero: never increase |position|, never flip (a flip becomes
  a close only).
- Sizing: target = allocation × weight (signed, truncated toward zero), clamped to allocation × leverage
  where leverage = min(user max, strategy max if given, platform cap, market max).
  delta = target − current. Skip (return []) if |delta| < max(min order $10, 2% of allocation), EXCEPT a
  full close (target 0), which always goes out so a flat signal never leaves a position open.
- Flips (sign change) are split into a reduce-only full close plus a separate opening plan. The open leg
  is only planned if the close leg is not liquidity-clamped and |target| ≥ the rebalance threshold.
- Liquidity: each order's notional is clamped to min(0.5% of 24h notional volume, 2% of open interest).
  If the clamp leaves less than the $10 minimum (and it is not a full close) → Rejection("liquidity_cap").
- Full closes are marked `close_position=True`: the executor must size them from the exact on-chain
  position (reduce-only), not from `sz`, and they are exempt from the $10 minimum (dust close).
  [VERIFY at go-live that Hyperliquid accepts reduce-only closes below $10.]
- Limit price: mid × (1 ± slippage cap), then rounded to Hyperliquid's price grid (≤5 significant figures,
  ≤ 6 − szDecimals decimals, integers always allowed) TOWARD the mid so the cap is never exceeded.
- The circuit breaker counter itself is the caller's: it should count exchange rejections of submitted
  orders (reset on an accepted order), not guard rejections here (market-wide conditions such as stale
  data would otherwise trip every subscription).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from collections.abc import Collection

from app.config import RiskLimits
from app.money import BPS, MICRO

from . import billing
from ._common import require_aware

__all__ = [
    "MarketSnapshot",
    "SubscriptionContext",
    "RiskFlags",
    "OrderPlan",
    "Rejection",
    "PERP_MAX_PX_DECIMALS",
    "FUTURE_SKEW_SECONDS",
    "MAX_WEIGHT_BPS",
    "round_px_toward_mid",
    "liquidity_cap_micro",
    "effective_max_leverage_x100",
    "plan_order",
]

PERP_MAX_PX_DECIMALS = 6
FUTURE_SKEW_SECONDS = 5
#: |weight| above 100x is treated as a corrupt signal and rejected; anything below is clamped by leverage.
MAX_WEIGHT_BPS = 100 * BPS


@dataclass(frozen=True)
class MarketSnapshot:
    coin: str
    mid_px: Decimal
    mark_px: Decimal
    oracle_px: Decimal
    day_ntl_vlm_micro: int
    open_interest_micro: int      # OI as USD notional (caller converts from coin units)
    max_leverage: int             # whole x, from Hyperliquid meta
    sz_decimals: int
    is_delisted: bool
    data_time: datetime           # when the snapshot was observed (tz-aware)


@dataclass(frozen=True)
class SubscriptionContext:
    allocation_micro: int
    max_leverage_x100: int                    # user's max leverage × 100 (200 = 2x)
    status: str                               # billing status
    current_position_notional_micro: int      # signed: + long, − short
    consecutive_rejects: int
    allowed_markets: Collection[str]          # strategy market whitelist
    past_due_since: datetime | None = None
    strategy_max_leverage_x100: int | None = None


@dataclass(frozen=True)
class RiskFlags:
    global_kill: bool = False
    killed_markets: frozenset[str] = field(default_factory=frozenset)
    new_entries_paused: bool = False
    paused_markets: frozenset[str] = field(default_factory=frozenset)   # per-market new-entry pause


@dataclass(frozen=True)
class OrderPlan:
    coin: str
    side: str                 # "buy" | "sell"
    notional_micro: int       # > 0 (for close_position: the position notional at planning time)
    sz: Decimal               # notional / mid, floored to sz_decimals (close_position: rounded up; executor
                              # should use the exact position size instead)
    limit_px: Decimal         # IOC limit, on the price grid, within the slippage cap of mid
    reduce_only: bool
    close_position: bool = False
    notes: tuple[str, ...] = ()

    @property
    def is_buy(self) -> bool:
        return self.side == "buy"


@dataclass(frozen=True)
class Rejection:
    reasons: tuple[str, ...]


def _sign(x: int) -> int:
    return (x > 0) - (x < 0)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_pos_dec(v: object) -> bool:
    return isinstance(v, Decimal) and v.is_finite() and v > 0


def round_px_toward_mid(px: Decimal, sz_decimals: int, is_buy: bool) -> Decimal:
    """Round a perp price to Hyperliquid's grid: ≤5 significant figures and ≤ (6 − szDecimals) decimals;
    integer prices are always valid. Buys round down, sells round up (toward the mid)."""
    max_dec = PERP_MAX_PX_DECIMALS - sz_decimals
    decimals = max(0, min(max_dec, 4 - px.adjusted()))
    q = Decimal(1).scaleb(-decimals)
    out = px.quantize(q, rounding=ROUND_FLOOR if is_buy else ROUND_CEILING)
    # rounding up can add a digit (9.99995 → 10.0000); re-apply the sig-fig limit if so
    decimals2 = max(0, min(max_dec, 4 - out.adjusted()))
    if decimals2 < decimals:
        out = out.quantize(Decimal(1).scaleb(-decimals2), rounding=ROUND_FLOOR if is_buy else ROUND_CEILING)
    # canonical form without exponent: integers as '100500', others without trailing zeros
    return out.quantize(Decimal(1)) if out == out.to_integral_value() else out.normalize()


def liquidity_cap_micro(market: MarketSnapshot, limits: RiskLimits) -> int:
    """Max notional of a single order: min(0.5% of 24h notional volume, 2% of open interest)."""
    by_vol = (market.day_ntl_vlm_micro * limits.max_order_pct_of_day_volume_bps) // BPS
    by_oi = (market.open_interest_micro * limits.max_order_pct_of_oi_bps) // BPS
    return max(0, min(by_vol, by_oi))


def effective_max_leverage_x100(ctx: SubscriptionContext, market: MarketSnapshot, limits: RiskLimits) -> int:
    caps = [ctx.max_leverage_x100, limits.platform_max_leverage * 100, market.max_leverage * 100]
    if ctx.strategy_max_leverage_x100 is not None:
        caps.append(ctx.strategy_max_leverage_x100)
    return min(caps)


def _validate(target_weight_bps: object, ctx: SubscriptionContext, market: MarketSnapshot,
              limits: RiskLimits, now: object) -> list[str]:
    bad: list[str] = []
    if not _is_int(target_weight_bps):
        bad.append("invalid_input:target_weight_bps")
    elif abs(target_weight_bps) > MAX_WEIGHT_BPS:  # type: ignore[arg-type]
        bad.append("weight_out_of_range")
    for name in ("allocation_micro", "consecutive_rejects"):
        v = getattr(ctx, name)
        if not _is_int(v) or v < 0:
            bad.append(f"invalid_input:{name}")
    if not _is_int(ctx.current_position_notional_micro):
        bad.append("invalid_input:current_position_notional_micro")
    if not _is_int(ctx.max_leverage_x100) or ctx.max_leverage_x100 <= 0:
        bad.append("invalid_input:max_leverage_x100")
    s = ctx.strategy_max_leverage_x100
    if s is not None and (not _is_int(s) or s <= 0):
        bad.append("invalid_input:strategy_max_leverage_x100")
    if ctx.past_due_since is not None and not _aware_ok(ctx.past_due_since):
        bad.append("invalid_input:past_due_since")
    if not isinstance(market.coin, str) or not market.coin:
        bad.append("invalid_input:coin")
    for name in ("mid_px", "mark_px", "oracle_px"):
        if not _is_pos_dec(getattr(market, name)):
            bad.append(f"invalid_input:{name}")
    for name in ("day_ntl_vlm_micro", "open_interest_micro"):
        v = getattr(market, name)
        if not _is_int(v) or v < 0:
            bad.append(f"invalid_input:{name}")
    if not _is_int(market.max_leverage) or market.max_leverage < 1:
        bad.append("invalid_input:max_leverage")
    if not _is_int(market.sz_decimals) or not 0 <= market.sz_decimals <= PERP_MAX_PX_DECIMALS:
        bad.append("invalid_input:sz_decimals")
    if not _aware_ok(market.data_time):
        bad.append("invalid_input:data_time")
    if not _aware_ok(now):
        bad.append("invalid_input:now")
    return bad


def _aware_ok(dt: object) -> bool:
    try:
        require_aware("dt", dt)
        return True
    except (TypeError, ValueError):
        return False


def _dev_bps(a: Decimal, ref: Decimal) -> Decimal:
    return abs(a - ref) * BPS / ref


def _hard_blocks(ctx: SubscriptionContext, market: MarketSnapshot, flags: RiskFlags,
                 limits: RiskLimits, now: datetime) -> list[str]:
    r: list[str] = []
    if flags.global_kill:
        r.append("kill_switch_global")
    if market.coin in flags.killed_markets:
        r.append("kill_switch_market")
    if ctx.consecutive_rejects >= limits.consecutive_reject_breaker:
        r.append("circuit_breaker_open")
    if not billing.exits_allowed(ctx.status):
        r.append(f"subscription_{ctx.status}" if ctx.status in billing.STATUSES else "subscription_status_unknown")
    if market.coin not in ctx.allowed_markets:
        r.append("market_not_whitelisted")
    if market.is_delisted:
        r.append("market_delisted")
    age = now - require_aware("data_time", market.data_time)
    if age > timedelta(seconds=limits.max_data_age_seconds):
        r.append("stale_data")
    elif age < -timedelta(seconds=FUTURE_SKEW_SECONDS):
        r.append("data_time_in_future")
    if _dev_bps(market.mark_px, market.oracle_px) > limits.max_mark_oracle_dev_bps:
        r.append("mark_oracle_deviation")
    if _dev_bps(market.mid_px, market.oracle_px) > limits.max_mark_oracle_dev_bps:
        r.append("mid_oracle_deviation")
    return r


def _sz(notional_micro: int, mid: Decimal, sz_decimals: int, round_up: bool) -> Decimal:
    raw = Decimal(notional_micro) / MICRO / mid
    return raw.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_CEILING if round_up else ROUND_FLOOR)


def plan_order(
    target_weight_bps: int,
    ctx: SubscriptionContext,
    market: MarketSnapshot,
    flags: RiskFlags,
    limits: RiskLimits | None,
    now: datetime,
    *,
    grace_hours: int = 72,
) -> list[OrderPlan] | Rejection:
    """Plan the orders moving this subscription's position in `market.coin` toward `target_weight_bps`
    (weight × 10_000 of allocation; negative = short). Returns [] when nothing should be done. See module doc."""
    limits = limits if limits is not None else RiskLimits()
    bad = _validate(target_weight_bps, ctx, market, limits, now)
    if bad:
        return Rejection(tuple(bad))
    now = require_aware("now", now)
    blocks = _hard_blocks(ctx, market, flags, limits, now)
    if blocks:
        return Rejection(tuple(blocks))

    notes: list[str] = []
    alloc, cur = ctx.allocation_micro, ctx.current_position_notional_micro

    # 1) target notional (signed, truncated toward zero), leverage clamp
    w = target_weight_bps
    target = _sign(w) * ((alloc * abs(w)) // BPS)
    max_abs = (alloc * effective_max_leverage_x100(ctx, market, limits)) // 100
    if abs(target) > max_abs:
        target = _sign(target) * max_abs
        notes.append("leverage_clamped")

    # 2) entry blocks → only exposure-reducing
    entries_ok = (
        billing.entries_allowed(ctx.status, ctx.past_due_since, now, grace_hours)
        and not flags.new_entries_paused
        and market.coin not in flags.paused_markets
    )
    if not entries_ok:
        clamped = target
        if cur == 0:
            clamped = 0
        elif target != 0 and _sign(target) != _sign(cur):
            clamped = 0                     # flip blocked → close only
        elif abs(target) > abs(cur):
            clamped = cur                   # increase blocked → hold
        if clamped != target:
            notes.append("entries_blocked")
            target = clamped

    delta = target - cur
    if delta == 0:
        return []

    threshold = max(limits.min_order_notional_micro, (alloc * limits.min_rebalance_pct_bps) // BPS)
    full_close = target == 0
    flip = cur != 0 and target != 0 and _sign(target) != _sign(cur)
    cap = liquidity_cap_micro(market, limits)

    # 3) legs: (side_sign, notional, reduce_only, close_position)
    legs: list[tuple[int, int, bool, bool]] = []
    if full_close or flip:
        legs.append((-_sign(cur), abs(cur), True, True))
        if flip:
            if abs(target) >= threshold:
                legs.append((_sign(target), abs(target), False, False))
            else:
                notes.append("open_leg_below_threshold")
    else:
        if abs(delta) < threshold:
            return []
        reduce = cur != 0 and _sign(delta) != _sign(cur)
        legs.append((_sign(delta), abs(delta), reduce, False))

    # 4) liquidity clamp, min order, price and size
    slip = Decimal(limits.max_slippage_bps) / BPS
    plans: list[OrderPlan] = []
    for i, (side_sign, notional, reduce_only, close_position) in enumerate(legs):
        leg_notes = list(notes)
        if notional > cap:
            notional, close_position = cap, False
            leg_notes.append("liquidity_clamped")
        if not close_position and notional < limits.min_order_notional_micro:
            if cap < limits.min_order_notional_micro or "liquidity_clamped" in leg_notes:
                return Rejection(("liquidity_cap",)) if not plans else plans
            continue  # open leg too small after all; nothing else to do
        is_buy = side_sign > 0
        raw_px = market.mid_px * (1 + slip) if is_buy else market.mid_px * (1 - slip)
        px = round_px_toward_mid(raw_px, market.sz_decimals, is_buy)
        if px <= 0:
            return Rejection(("price_unrepresentable",)) if not plans else plans
        sz = _sz(notional, market.mid_px, market.sz_decimals, round_up=close_position)
        if sz <= 0:
            if close_position:
                sz = Decimal(1).scaleb(-market.sz_decimals)  # smallest lot; reduce-only caps at the position
            else:
                continue
        plans.append(OrderPlan(market.coin, "buy" if is_buy else "sell", notional, sz, px,
                               reduce_only, close_position, tuple(leg_notes)))
        if i == 0 and len(legs) > 1 and not close_position:
            break  # flip's close leg was clamped: do not open the other side this tick
    return plans
