"""Adapters from the execution ports to the pure ``app.domain`` modules and the notifier.

Postgres adapters live in ``app.execution.pg``, the KMS key provider in ``app.execution.keys``; everything is composed
in ``app.execution.jobs``. This file covers the pure pieces plus the ONE mapping between the two market-snapshot
types:

- ``app.domain.risk.MarketSnapshot`` — produced by ``app.hl.markets.MarketCatalog.to_snapshot`` and consumed by
  ``risk.plan_order`` (field names ``day_ntl_vlm_micro`` / ``open_interest_micro`` / ``data_time``);
- ``app.execution.ports.MarketSnapshot`` — the executor's port type (``day_notional_volume_micro`` /
  ``open_interest_notional_micro`` / ``as_of``).

``to_risk_snapshot`` / ``from_risk_snapshot`` are the only conversions (lossless, round-trip tested);
``CatalogMarketData`` is the executor's ``MarketData`` over any catalog source (``app.hl.readers.HlMarketData``).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Sequence

from app.config import Economics, RiskLimits
from app.domain import billing as _billing
from app.domain import fees as _fees
from app.domain import jitter as _jitter
from app.domain import profit_share as _ps
from app.domain import risk as _risk
from app.errors import GuardRejected, NotFound
from app.logging import get_logger

from .ports import (
    CLOSING_STATUS,
    AlertEvent,
    BuilderFeeSplit,
    MarketSnapshot,
    OrderLeg,
    OrderPlan,
    PlanInput,
    ProfitShareCharge,
)

log = get_logger("app.execution.wiring")

# risk.plan_order reason codes that describe the market / platform, not the subscription.
MARKET_SCOPE_REASONS = frozenset({
    "kill_switch_global", "kill_switch_market", "market_delisted", "stale_data", "data_time_in_future",
    "mark_oracle_deviation", "mid_oracle_deviation", "liquidity_cap", "price_unrepresentable",
})


def to_risk_snapshot(snap: MarketSnapshot) -> _risk.MarketSnapshot:
    """Executor port snapshot → ``app.domain.risk.MarketSnapshot`` (what the guards read)."""
    return _risk.MarketSnapshot(
        coin=snap.coin, mid_px=snap.mid_px, mark_px=snap.mark_px, oracle_px=snap.oracle_px,
        day_ntl_vlm_micro=snap.day_notional_volume_micro, open_interest_micro=snap.open_interest_notional_micro,
        max_leverage=snap.max_leverage, sz_decimals=snap.sz_decimals, is_delisted=snap.is_delisted,
        data_time=snap.as_of)


def from_risk_snapshot(snap: _risk.MarketSnapshot) -> MarketSnapshot:
    """``app.domain.risk.MarketSnapshot`` (``MarketCatalog.to_snapshot``) → executor port snapshot."""
    return MarketSnapshot(
        coin=snap.coin, mid_px=snap.mid_px, mark_px=snap.mark_px, oracle_px=snap.oracle_px,
        day_notional_volume_micro=snap.day_ntl_vlm_micro, open_interest_notional_micro=snap.open_interest_micro,
        max_leverage=snap.max_leverage, sz_decimals=snap.sz_decimals, as_of=snap.data_time,
        is_delisted=snap.is_delisted)


class CatalogMarketData:
    """``MarketData`` port over ``source.catalog(coins) -> app.hl.markets.MarketCatalog`` (``HlMarketData`` caches
    metaAndAssetCtxs per dex). Unknown market / no mid → None (the executor waits and retries next tick);
    transport failures propagate as ``ExternalServiceError`` (the executor treats them as "no data")."""

    def __init__(self, source: Any) -> None:
        self.source = source

    def snapshot(self, coin: str) -> MarketSnapshot | None:
        try:
            snap = self.source.catalog([coin]).to_snapshot(coin)
        except (NotFound, GuardRejected) as e:
            log.info("market_snapshot_unavailable", extra={"fields": {"coin": coin, "reason": e.message}})
            return None
        return from_risk_snapshot(snap)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class RiskPlanner:
    """OrderPlanner over ``app.domain.risk.plan_order``."""

    def __init__(self, limits: RiskLimits | None = None, grace_hours: int = 72) -> None:
        self.limits = limits or RiskLimits()
        self.grace_hours = grace_hours

    def plan(self, inp: PlanInput) -> OrderPlan:
        sub, snap, pos, flags = inp.subscription, inp.snapshot, inp.position, inp.flags
        # SPEC §12: a closing subscription may only reduce (the billing state machine has no "closing" state;
        # "reduce_only" is exactly its trading semantics: exits allowed, entries blocked).
        status = "reduce_only" if sub.status == CLOSING_STATUS else sub.status
        ctx = _risk.SubscriptionContext(
            allocation_micro=sub.allocation_micro, max_leverage_x100=sub.max_leverage_x100, status=status,
            current_position_notional_micro=pos.notional_micro, consecutive_rejects=sub.consecutive_rejections,
            allowed_markets=frozenset(sub.markets), past_due_since=sub.past_due_since,
            strategy_max_leverage_x100=sub.strategy_max_leverage_x100)
        market = to_risk_snapshot(snap)
        paused = set(flags.paused_entry_markets)
        if inp.reduce_only_mode:
            paused.add(inp.coin)  # executor-level reduce-only (status / flags) → entries blocked on this coin
        rflags = _risk.RiskFlags(global_kill=flags.kill_switch_global, killed_markets=frozenset(flags.killed_markets),
                                 new_entries_paused=flags.new_entries_paused, paused_markets=frozenset(paused))
        out = _risk.plan_order(inp.weight_bps, ctx, market, rflags, self.limits, inp.now, grace_hours=self.grace_hours)
        if isinstance(out, _risk.Rejection):
            reasons = tuple(out.reasons)
            scope = "market" if reasons and all(r in MARKET_SCOPE_REASONS for r in reasons) else "subscription"
            raise GuardRejected(",".join(reasons), reasons=reasons, scope=scope)
        target = pos.notional_micro
        legs = []
        for p in out:
            target += p.notional_micro if p.is_buy else -p.notional_micro
            legs.append(OrderLeg(is_buy=p.is_buy, sz=p.sz, limit_px=p.limit_px, reduce_only=p.reduce_only,
                                 close_position=p.close_position, notional_micro=p.notional_micro))
        notes = tuple(n for p in out for n in p.notes)
        return OrderPlan(coin=inp.coin, target_notional_micro=target, legs=tuple(legs), notes=notes)


class DomainJitter:
    def __init__(self, salt: bytes, jitter_max_seconds: int) -> None:
        self.salt = salt
        self.max = jitter_max_seconds

    def delay_seconds(self, user_id: str, bar_close: datetime) -> int:
        return _jitter.delay_seconds(user_id, bar_close, self.salt, self.max)

    def fair_order(self, subscription_ids: Sequence[str], bar_close: datetime) -> list[str]:
        return _jitter.fair_order(subscription_ids, _jitter.bar_seed(self.salt, bar_close))


class DomainProfitShare:
    def __init__(self, economics: Economics | None = None) -> None:
        self.economics = economics or Economics()

    def settle(self, *, cum_pnl_micro: int, hwm_micro: int, pnl_delta_micro: int, creator_bps: int,
               in_house: bool) -> ProfitShareCharge:
        r = _ps.settle(_ps.SubscriptionPnlState(cum_pnl_micro, hwm_micro), pnl_delta_micro, creator_bps,
                       self.economics, in_house=in_house)
        return ProfitShareCharge(creator_micro=r.creator_part, platform_micro=r.platform_part,
                                 new_cum_pnl_micro=r.new_state.cum_pnl_micro, new_hwm_micro=r.new_state.hwm_micro,
                                 profit_micro=r.profit)


class DomainFees:
    def __init__(self, economics: Economics | None = None) -> None:
        self.economics = economics or Economics()

    def split_builder_fee(self, fee_micro: int, *, in_house: bool, referrer_share_bps: int | None) -> BuilderFeeSplit:
        d = _fees.split_builder_fee(fee_micro, in_house, referrer_share_bps, self.economics)
        return BuilderFeeSplit(creator_micro=d["creator"], platform_micro=d["platform"], referrer_micro=d["referrer"])

    def split_subscription(self, price_micro: int) -> tuple[int, int]:
        return _fees.subscription_split(price_micro, self.economics)


class DomainBilling:
    def __init__(self, grace_hours: int = 72) -> None:
        self._grace = grace_hours

    @property
    def grace_hours(self) -> int:
        return self._grace

    def next_status(self, *, status: str, balance_micro: int, amount_due_micro: int,
                    past_due_since: datetime | None, now: datetime) -> tuple[str, datetime | None]:
        d = _billing.next_status(status, balance_micro, amount_due_micro, past_due_since, now, self._grace)
        return d.status, d.past_due_since

    def next_period_end(self, anchor: datetime, now: datetime) -> datetime:
        return _billing.next_renewal_after(anchor, now)


class NotifierAlertSink:
    """AlertSink over ``app.alerts.notifier.Notifier`` (never raises)."""

    def __init__(self, notifier: Any) -> None:
        self.notifier = notifier

    def emit(self, alert: AlertEvent) -> None:
        from app.alerts import notifier as _n

        data = dict(alert.payload)
        if alert.kind not in _n.TEMPLATES and "detail" not in data:
            data["detail"] = "; ".join(f"{k}={v}" for k, v in sorted(data.items()))
        self.notifier.notify(_n.Alert(kind=alert.kind, severity=_n.Severity(alert.severity), user_id=alert.user_id,
                                      coin=alert.coin, data=data, key=alert.dedup_key))
