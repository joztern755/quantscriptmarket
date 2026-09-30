"""Executor tick (SPEC §2 ``/internal/tick``, §5.4, §5.8, §10).

``Executor.run_tick(now)`` is called every minute by Cloud Scheduler. One tick:

1. Reads flags once. Global kill switch → nothing is placed (not even exits).
2. For every listed strategy version, takes its latest ``bar_close`` signal (stale > 36h → alert, skip).
3. Loads the subscriptions of that version not yet done for that bar, orders them with ``jitter.fair_order``
   (deterministic shuffle per bar) and keeps only those whose jittered due time ``bar_close + delay`` has passed.
   Versions are interleaved round-robin so a large strategy cannot starve a small one.
4. Processes subscriptions until the time budget or the per-tick cap is reached (the rest are picked up next tick).
   Each subscription runs under a non-blocking advisory lock (concurrent ticks never touch the same subscription)
   and inside its own ``try`` (one user's failure never stops the others).

Per subscription:
- Phase 1 (no key material): resolve orders whose outcome is unknown (crash/timeout between send and record) via the
  info endpoint, count attempts, apply market kill switches / whitelist / reduce-only, plan each coin with the
  pure planner (``domain.risk.plan_order``) against the *on-chain* position.
- Phase 2: only if something must be sent, open the agent key (zeroized on exit), build a gateway and place IOC
  orders. Each order's intent row is inserted *before* sending, keyed by a deterministic cloid
  (subscription, bar, coin, attempt): a retry of the same attempt can never place a second order, and an unknown
  outcome is resolved (never re-sent blindly) on the next tick.
- Partial fills: the residual is re-planned from the actual position on a later tick, up to
  ``max_attempts_per_bar`` submitted orders per (subscription, bar, coin). Then the bar is closed as "residual".
- Circuit breaker: ``consecutive_reject_breaker`` consecutive rejections (guard, exchange, unexpected exception) stop
  the subscription until an admin resets the counter. Market-scope guard failures and exchange/info outages do not
  count (they are not the subscription's fault and would otherwise trip every subscriber at once).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from app.errors import ExternalServiceError, GuardRejected
from app.logging import get_logger

from .ports import (
    ORDER_FILLED,
    ORDER_NOT_SUBMITTED,
    ORDER_PARTIAL,
    ORDER_REJECTED,
    ORDER_RESTING,
    ORDER_SUBMITTING,
    ORDER_UNKNOWN,
    PENDING_ORDER_STATUSES,
    TRADABLE_STATUSES,
    AlertEvent,
    AlertSink,
    BarSignal,
    Clock,
    FlagRepo,
    Flags,
    GatewayFactory,
    Jitter,
    KeyProvider,
    LockProvider,
    MarketData,
    MarketSnapshot,
    OrderPlan,
    OrderPlanner,
    OrderRecord,
    OrderStatusReader,
    PlaceResult,
    PlanInput,
    Position,
    PositionReader,
    SignalRepo,
    SubscriptionRepo,
    SubscriptionView,
)

log = get_logger("app.execution.executor")

# First 4 bytes of every cloid we place. hl/fills.py attributes fills to subscriptions by this prefix (SPEC §1.1).
CLOID_PREFIX = "a17a1000"

_RESULT_TO_STATUS = {
    "filled": ORDER_FILLED,
    "partial": ORDER_PARTIAL,
    "rejected": ORDER_REJECTED,
    "resting": ORDER_RESTING,
    "unknown": ORDER_UNKNOWN,
}
# Statuses re-queried on the next tick before anything new is sent (IOC should never rest; treat it as unresolved).
_UNRESOLVED = PENDING_ORDER_STATUSES + (ORDER_RESTING,)


def make_cloid(subscription_id: str, bar_close: datetime, coin: str, attempt: int) -> str:
    """Deterministic 128-bit Hyperliquid client order id: ``0x`` + prefix(8 hex) + sha256(...)[:24 hex]."""
    material = f"{subscription_id}|{bar_close.isoformat()}|{coin}|{attempt}".encode()
    return "0x" + CLOID_PREFIX + hashlib.sha256(material).hexdigest()[:24]


def is_our_cloid(cloid: str | None) -> bool:
    return bool(cloid) and cloid.lower().startswith("0x" + CLOID_PREFIX)


@dataclass(frozen=True)
class ExecutorConfig:
    max_attempts_per_bar: int = 3            # submitted orders per (subscription, bar, coin)
    time_budget_seconds: float = 45.0        # stop starting new subscriptions after this (tick runs every 60s)
    max_subscriptions_per_tick: int = 200
    max_due_fetch_per_version: int = 2000
    breaker_threshold: int = 3               # RiskLimits.consecutive_reject_breaker
    signal_max_age_seconds: int = 36 * 3600  # RiskLimits.signal_max_age_hours
    unknown_order_grace_seconds: int = 120   # an unseen cloid younger than this may still be in flight
    lock_prefix: str = "exec:sub:"

    @classmethod
    def from_settings(cls, settings: Any, **overrides: Any) -> "ExecutorConfig":
        risk = settings.risk
        base = cls(breaker_threshold=risk.consecutive_reject_breaker,
                   signal_max_age_seconds=risk.signal_max_age_hours * 3600)
        return replace(base, **overrides)


@dataclass
class TickReport:
    started_at: datetime
    kill_switch_global: bool = False
    signals_seen: int = 0
    stale_signals: int = 0
    due: int = 0
    not_due: int = 0
    processed: int = 0
    deferred: int = 0
    locked: int = 0
    already_done: int = 0
    breaker_open: int = 0
    bars_completed: int = 0
    orders_placed: int = 0
    orders_filled: int = 0
    orders_partial: int = 0
    orders_rejected: int = 0
    orders_unknown: int = 0
    guard_rejections: int = 0
    market_rejections: int = 0
    market_killed: int = 0
    reduce_only_skips: int = 0
    no_market_data: int = 0
    unresolved_orders: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0
    error_subscriptions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["started_at"] = self.started_at.isoformat()
        return d


@dataclass
class _Tick:
    """Per-tick scratch state."""
    now: datetime
    flags: Flags
    report: TickReport
    snapshots: dict[str, MarketSnapshot | None] = field(default_factory=dict)


@dataclass(frozen=True)
class _PendingOrder:
    coin: str
    plan: OrderPlan
    attempt: int


class _BreakerTripped(Exception):
    pass


class Executor:
    def __init__(
        self,
        *,
        signals: SignalRepo,
        subscriptions: SubscriptionRepo,
        market_data: MarketData,
        positions: PositionReader,
        order_status: OrderStatusReader,
        keys: KeyProvider,
        gateways: GatewayFactory,
        flags: FlagRepo,
        alerts: AlertSink,
        clock: Clock,
        locks: LockProvider,
        planner: OrderPlanner,
        jitter: Jitter,
        config: ExecutorConfig | None = None,
    ) -> None:
        self.signals = signals
        self.subs = subscriptions
        self.market_data = market_data
        self.positions = positions
        self.order_status = order_status
        self.keys = keys
        self.gateways = gateways
        self.flags = flags
        self.alerts = alerts
        self.clock = clock
        self.locks = locks
        self.planner = planner
        self.jitter = jitter
        self.cfg = config or ExecutorConfig()

    # ------------------------------------------------------------------------------------------------------------ tick

    def run_tick(self, now: datetime | None = None) -> TickReport:
        now = now or self.clock.now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware UTC")
        t0 = self.clock.monotonic()
        report = TickReport(started_at=now)
        flags = self.flags.flags()
        tick = _Tick(now=now, flags=flags, report=report)

        if flags.kill_switch_global:
            report.kill_switch_global = True
            log.warning("tick_skipped_kill_switch", extra={"fields": {"now": now.isoformat()}})
            report.elapsed_seconds = self.clock.monotonic() - t0
            return report

        queues: list[list[tuple[SubscriptionView, BarSignal, int]]] = []
        for sig in self.signals.latest_signals():
            report.signals_seen += 1
            q = self._due_for_signal(sig, tick)
            if q:
                queues.append(q)
        work = _round_robin(queues)
        report.due = len(work)

        for i, (sub, sig, delay) in enumerate(work):
            if report.processed >= self.cfg.max_subscriptions_per_tick or (
                self.clock.monotonic() - t0 >= self.cfg.time_budget_seconds
            ):
                report.deferred = len(work) - i
                break
            report.processed += 1
            try:
                self._run_subscription(sub, sig, delay, tick)
            except Exception as exc:  # isolation: one subscription never stops the others
                self._on_subscription_error(sub, sig, exc, tick)

        report.elapsed_seconds = self.clock.monotonic() - t0
        log.info("tick_done", extra={"fields": report.as_dict()})
        return report

    def _due_for_signal(self, sig: BarSignal, tick: _Tick) -> list[tuple[SubscriptionView, BarSignal, int]]:
        now, report = tick.now, tick.report
        if sig.bar_close > now:
            return []
        age = (now - sig.bar_close).total_seconds()
        if age > self.cfg.signal_max_age_seconds:
            report.stale_signals += 1
            self._alert("warn", "stale_signal", {
                "strategy_version_id": sig.strategy_version_id, "bar_close": sig.bar_close.isoformat(),
                "age_hours": round(age / 3600, 1)}, dedup=f"stale_signal:{sig.strategy_version_id}:{sig.bar_close.isoformat()}")
            return []
        subs = self.subs.due_subscriptions(sig.strategy_version_id, sig.bar_close, self.cfg.max_due_fetch_per_version)
        by_id = {s.id: s for s in subs}
        out = []
        for sid in self.jitter.fair_order(list(by_id), sig.bar_close):
            sub = by_id[sid]
            delay = int(self.jitter.delay_seconds(sid, sig.bar_close))
            if now < sig.bar_close + timedelta(seconds=delay):
                report.not_due += 1
                continue
            out.append((sub, sig, delay))
        return out

    # ---------------------------------------------------------------------------------------------------- subscription

    def _run_subscription(self, sub: SubscriptionView, sig: BarSignal, delay: int, tick: _Tick) -> None:
        report = tick.report
        with self.locks.try_lock(self.cfg.lock_prefix + sub.id) as held:
            if not held:
                report.locked += 1
                return
            # Re-check under the lock: a concurrent tick may have finished this bar meanwhile.
            if self.subs.is_bar_done(sub.id, sig.bar_close):
                report.already_done += 1
                return
            if sub.status not in TRADABLE_STATUSES:
                return
            if sub.consecutive_rejections >= self.cfg.breaker_threshold:
                report.breaker_open += 1
                return

            reduce_only_sub = sub.status == "reduce_only" or tick.flags.new_entries_paused
            coins = sorted(sig.weights_bps)
            positions = self.positions.positions(sub.trading_address, coins)

            all_done = True
            residual = False
            to_place: list[_PendingOrder] = []
            try:
                for coin in coins:
                    state, pending = self._prepare_coin(sub, sig, coin, positions.get(coin) or Position.flat(coin),
                                                        reduce_only_sub, tick)
                    if pending is not None:
                        to_place.append(pending)
                    elif state == "residual":
                        residual = True
                    elif state != "done":
                        all_done = False

                if to_place:
                    for placed_done in self._place_all(sub, sig, delay, to_place, tick):
                        if not placed_done:
                            all_done = False
            except _BreakerTripped:
                return

            if all_done:
                self.subs.mark_bar_done(sub.id, sig.bar_close, "residual" if residual else "ok")
                report.bars_completed += 1

    def _prepare_coin(self, sub: SubscriptionView, sig: BarSignal, coin: str, pos: Position, reduce_only_sub: bool,
                      tick: _Tick) -> tuple[str, _PendingOrder | None]:
        """Returns (state, order_to_place). state: done | residual | wait."""
        report, flags = tick.report, tick.flags
        weight = int(sig.weights_bps[coin])

        if coin not in sub.markets:
            # Fail closed: never trade a coin outside the strategy whitelist, whatever the signal says.
            self._alert("critical", "signal_market_not_whitelisted", {
                "strategy_version_id": sig.strategy_version_id, "coin": coin, "bar_close": sig.bar_close.isoformat()},
                dedup=f"not_whitelisted:{sig.strategy_version_id}:{sig.bar_close.isoformat()}:{coin}")
            return "done", None
        if coin in flags.killed_markets:
            report.market_killed += 1
            return "wait", None

        orders = list(self.subs.orders_for_bar(sub.id, sig.bar_close, coin))
        if any(o.status in _UNRESOLVED for o in orders):
            for o in orders:
                if o.status in _UNRESOLVED:
                    self._resolve_order(sub, o, tick)
            orders = list(self.subs.orders_for_bar(sub.id, sig.bar_close, coin))
            if any(o.status in _UNRESOLVED for o in orders):
                report.unresolved_orders += 1
                return "wait", None

        submitted = [o for o in orders if o.status != ORDER_NOT_SUBMITTED]
        if len(submitted) >= self.cfg.max_attempts_per_bar or len(orders) >= 2 * self.cfg.max_attempts_per_bar:
            last = orders[-1] if orders else None
            if last is not None and last.status != ORDER_FILLED:
                self._alert("warn", "execution_residual", {
                    "subscription_id": sub.id, "coin": coin, "bar_close": sig.bar_close.isoformat(),
                    "attempts": len(submitted), "last_status": last.status},
                    user_id=sub.user_id, dedup=f"residual:{sub.id}:{sig.bar_close.isoformat()}:{coin}")
                return "residual", None
            return "done", None

        snap = self._snapshot(coin, tick)
        if snap is None:
            report.no_market_data += 1
            return "wait", None

        reduce_only = reduce_only_sub or coin in flags.paused_entry_markets
        try:
            plan = self.planner.plan(PlanInput(subscription=sub, coin=coin, weight_bps=weight, position=pos,
                                               snapshot=snap, reduce_only_mode=reduce_only, now=tick.now))
        except GuardRejected as exc:
            if (exc.details or {}).get("scope") == "market":
                report.market_rejections += 1
                self._alert("warn", "guard_rejected_market", {"coin": coin, "reason": exc.message},
                            dedup=f"guard_market:{coin}:{sig.bar_close.isoformat()}:{exc.message}")
                return "wait", None
            report.guard_rejections += 1
            self._count_rejection(sub, f"guard:{exc.message}", coin, tick)
            return "wait", None

        if reduce_only:
            plan = _enforce_reduce_only(plan, pos)
            if plan.action == "skip" and plan.reason == "reduce_only_blocks_increase":
                report.reduce_only_skips += 1
        self.subs.record_target(sub.id, coin, sig.bar_close, plan.target_notional_micro, weight)

        if plan.action != "order" or plan.sz <= 0:
            return "done", None
        return "wait", _PendingOrder(coin=coin, plan=plan, attempt=len(orders))

    # ------------------------------------------------------------------------------------------------------ placement

    def _place_all(self, sub: SubscriptionView, sig: BarSignal, delay: int, orders: list[_PendingOrder],
                   tick: _Tick) -> list[bool]:
        vault = sub.trading_address if sub.trading_address.lower() != sub.master_address.lower() else None
        results: list[bool] = []
        with self.keys.agent_key(sub.user_id, sub.master_address) as key:
            gw = self.gateways.create(key, sub.master_address, vault)
            for p in orders:
                results.append(self._place_one(gw, sub, sig, delay, p, tick))
        return results

    def _place_one(self, gw: Any, sub: SubscriptionView, sig: BarSignal, delay: int, p: _PendingOrder,
                   tick: _Tick) -> bool:
        """Returns True when the coin is done for this bar."""
        report, plan = tick.report, p.plan
        cloid = make_cloid(sub.id, sig.bar_close, p.coin, p.attempt)
        if self.subs.get_order(cloid) is not None:
            return False  # already recorded (concurrent/previous attempt) — resolved next tick, never re-sent
        rec = OrderRecord(
            subscription_id=sub.id, strategy_version_id=sig.strategy_version_id, bar_close=sig.bar_close,
            attempt=p.attempt, cloid=cloid, coin=p.coin, is_buy=plan.is_buy, sz=plan.sz, limit_px=plan.limit_px,
            reduce_only=plan.reduce_only, status=ORDER_SUBMITTING, jitter_seconds=delay, submitted_at=tick.now)
        if not self.subs.insert_order(rec):
            return False
        fields = {"subscription_id": sub.id, "coin": p.coin, "cloid": cloid, "attempt": p.attempt,
                  "is_buy": plan.is_buy, "sz": str(plan.sz), "limit_px": str(plan.limit_px),
                  "reduce_only": plan.reduce_only, "bar_close": sig.bar_close.isoformat()}
        try:
            res = gw.place_ioc(coin=p.coin, is_buy=plan.is_buy, sz=plan.sz, limit_px=plan.limit_px,
                               reduce_only=plan.reduce_only, cloid=cloid)
        except Exception as exc:  # outcome unknown: keep the row pending; next tick queries by cloid
            report.orders_unknown += 1
            self.subs.update_order(cloid, status=ORDER_UNKNOWN, filled_sz=Decimal(0), avg_px=None, oid=None,
                                   error=type(exc).__name__, hl_response={})
            log.warning("order_outcome_unknown", extra={"fields": {**fields, "error": type(exc).__name__}})
            return False
        report.orders_placed += 1
        return self._apply_result(sub, cloid, p.coin, plan.sz, res, tick, fields)

    def _apply_result(self, sub: SubscriptionView, cloid: str, coin: str, sz: Decimal, res: PlaceResult, tick: _Tick,
                      fields: dict[str, Any]) -> bool:
        report = tick.report
        status = _RESULT_TO_STATUS.get(res.status, ORDER_UNKNOWN)
        if status == ORDER_FILLED and res.filled_sz < sz:
            status = ORDER_PARTIAL
        self.subs.update_order(cloid, status=status, filled_sz=res.filled_sz, avg_px=res.avg_px, oid=res.oid,
                               error=res.error, hl_response=dict(res.raw))
        log.info("order_result", extra={"fields": {**fields, "status": status, "filled_sz": str(res.filled_sz),
                                                   "error": res.error}})
        if status == ORDER_FILLED:
            report.orders_filled += 1
            self.subs.reset_rejections(sub.id)
            return True
        if status == ORDER_PARTIAL:
            report.orders_partial += 1
            if res.filled_sz > 0:
                self.subs.reset_rejections(sub.id)
            return False
        if status == ORDER_REJECTED:
            report.orders_rejected += 1
            self._count_rejection(sub, f"exchange:{res.error or 'rejected'}", coin, tick)
            return False
        report.orders_unknown += 1
        return False

    def _resolve_order(self, sub: SubscriptionView, o: OrderRecord, tick: _Tick) -> None:
        res = self.order_status.order_status_by_cloid(sub.trading_address, o.cloid)
        if res is None or res.status == "unknown":
            age = (tick.now - o.submitted_at).total_seconds()
            if res is None and age >= self.cfg.unknown_order_grace_seconds:
                self.subs.update_order(o.cloid, status=ORDER_NOT_SUBMITTED, filled_sz=Decimal(0), avg_px=None,
                                       oid=None, error="not_found_on_exchange", hl_response={})
                log.info("order_resolved_not_submitted", extra={"fields": {"subscription_id": sub.id, "cloid": o.cloid}})
            elif age >= 10 * self.cfg.unknown_order_grace_seconds:
                self._alert("warn", "order_unresolved", {"subscription_id": sub.id, "cloid": o.cloid, "coin": o.coin},
                            user_id=sub.user_id, dedup=f"unresolved:{o.cloid}")
            return
        if res.status == "resting":
            self._alert("warn", "ioc_order_resting", {"subscription_id": sub.id, "cloid": o.cloid, "coin": o.coin},
                        dedup=f"resting:{o.cloid}")
            return
        fields = {"subscription_id": sub.id, "coin": o.coin, "cloid": o.cloid, "resolved": True}
        self._apply_result(sub, o.cloid, o.coin, o.sz, res, tick, fields)

    # --------------------------------------------------------------------------------------------------------- helpers

    def _snapshot(self, coin: str, tick: _Tick) -> MarketSnapshot | None:
        if coin not in tick.snapshots:
            try:
                tick.snapshots[coin] = self.market_data.snapshot(coin)
            except ExternalServiceError as exc:
                log.warning("market_data_unavailable", extra={"fields": {"coin": coin, "error": exc.message}})
                tick.snapshots[coin] = None
        return tick.snapshots[coin]

    def _count_rejection(self, sub: SubscriptionView, reason: str, coin: str | None, tick: _Tick) -> None:
        count = self.subs.record_rejection(sub.id, reason)
        self._alert("warn", "order_rejected", {"subscription_id": sub.id, "coin": coin, "reason": reason,
                                               "consecutive": count}, user_id=sub.user_id)
        if count >= self.cfg.breaker_threshold:
            tick.report.breaker_open += 1
            self._alert("critical", "circuit_breaker_open", {"subscription_id": sub.id, "consecutive": count,
                                                             "last_reason": reason},
                        user_id=sub.user_id, dedup=f"breaker:{sub.id}")
            log.warning("circuit_breaker_open", extra={"fields": {"subscription_id": sub.id, "consecutive": count}})
            raise _BreakerTripped()

    def _on_subscription_error(self, sub: SubscriptionView, sig: BarSignal, exc: Exception, tick: _Tick) -> None:
        report = tick.report
        report.errors += 1
        report.error_subscriptions.append(sub.id)
        log.error("subscription_failed", exc_info=True, extra={"fields": {
            "subscription_id": sub.id, "strategy_version_id": sig.strategy_version_id, "error": type(exc).__name__}})
        try:
            self._alert("warn", "execution_error", {"subscription_id": sub.id, "error": type(exc).__name__},
                        user_id=sub.user_id, dedup=f"exec_error:{sub.id}:{sig.bar_close.isoformat()}")
            if not isinstance(exc, ExternalServiceError):
                try:
                    self._count_rejection(sub, f"exception:{type(exc).__name__}", None, tick)
                except _BreakerTripped:
                    pass
        except Exception:
            log.error("subscription_error_handling_failed", exc_info=True, extra={"fields": {"subscription_id": sub.id}})

    def _alert(self, severity: str, kind: str, payload: dict[str, Any], *, user_id: str | None = None,
               dedup: str | None = None, coin: str | None = None) -> None:
        try:
            self.alerts.emit(AlertEvent(severity=severity, kind=kind, payload=payload, user_id=user_id,
                                        dedup_key=dedup, coin=coin))
        except Exception:  # alerting must never break execution
            log.error("alert_emit_failed", exc_info=True, extra={"fields": {"kind": kind}})


def _enforce_reduce_only(plan: OrderPlan, pos: Position) -> OrderPlan:
    """Defence in depth on top of the planner: in reduce-only mode exposure may only shrink, never flip."""
    n, t = pos.notional_micro, plan.target_notional_micro
    if n == 0:
        eff_target = 0
    elif (n > 0 and 0 <= t <= n) or (n < 0 and n <= t <= 0):
        eff_target = t
    elif (n > 0 and t < 0) or (n < 0 and t > 0):
        eff_target = 0
    else:
        eff_target = n
    if plan.action != "order":
        return replace(plan, target_notional_micro=eff_target)
    increases = (plan.is_buy and pos.szi >= 0) or ((not plan.is_buy) and pos.szi <= 0)
    if increases:
        return replace(plan, action="skip", sz=Decimal(0), target_notional_micro=eff_target,
                       reason="reduce_only_blocks_increase")
    return replace(plan, sz=min(plan.sz, abs(pos.szi)), reduce_only=True, target_notional_micro=eff_target)


def _round_robin(queues: list[list[Any]]) -> list[Any]:
    out: list[Any] = []
    i = 0
    while True:
        added = False
        for q in queues:
            if i < len(q):
                out.append(q[i])
                added = True
        if not added:
            return out
        i += 1
