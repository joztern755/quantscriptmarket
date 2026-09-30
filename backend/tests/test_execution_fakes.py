"""In-memory fakes of the execution ports (shared by the test_execution_* modules) + sanity tests of the fakes."""
from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import Conflict  # noqa: E402
from app.execution.ports import (  # noqa: E402
    TRADABLE_STATUSES,
    AlertEvent,
    BarSignal,
    BuilderFeeFill,
    ExpectedPosition,
    Flags,
    LedgerLine,
    MarketSnapshot,
    OrderRecord,
    PlaceResult,
    PlanAccount,
    PnlDelta,
    Position,
    SettlementSubscription,
    SubscriptionView,
)
from app.money import MICRO  # noqa: E402

import logging  # noqa: E402

logging.getLogger("app.execution").setLevel(logging.CRITICAL)  # keep test output readable (assertLogs overrides)

UTC = timezone.utc
BAR = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
SILVER = "xyz:SILVER"


def usd(x: int | str) -> int:
    return int(Decimal(str(x)) * MICRO)


# ------------------------------------------------------------------------------------------------ execution fakes

class FakeClock:
    def __init__(self, now: datetime = BAR) -> None:
        self._now = now
        self.mono = 0.0
        self.mono_step = 0.0   # added on every monotonic() call (simulates slow work)

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now

    def monotonic(self) -> float:
        self.mono += self.mono_step
        return self.mono


class FakeSignals:
    def __init__(self, signals: Sequence[BarSignal] = ()) -> None:
        self.signals = list(signals)

    def latest_signals(self) -> Sequence[BarSignal]:
        return list(self.signals)


class FakeSubRepo:
    def __init__(self, subs: Iterable[SubscriptionView] = ()) -> None:
        self.subs: dict[str, SubscriptionView] = {s.id: s for s in subs}
        self.done: dict[tuple[str, datetime], str] = {}
        self.orders: dict[str, OrderRecord] = {}
        self.rejections: dict[str, int] = {}
        self.targets: dict[tuple[str, str], tuple[datetime, int, int]] = {}
        self.fail_on_due = False

    def add(self, sub: SubscriptionView) -> None:
        self.subs[sub.id] = sub

    def _view(self, s: SubscriptionView) -> SubscriptionView:
        return replace(s, consecutive_rejections=self.rejections.get(s.id, 0))

    def due_subscriptions(self, strategy_version_id: str, bar_close: datetime, limit: int) -> Sequence[SubscriptionView]:
        out = [self._view(s) for s in self.subs.values()
               if s.strategy_version_id == strategy_version_id and s.status in TRADABLE_STATUSES
               and (s.id, bar_close) not in self.done]
        return out[:limit]

    def is_bar_done(self, subscription_id: str, bar_close: datetime) -> bool:
        return (subscription_id, bar_close) in self.done

    def mark_bar_done(self, subscription_id: str, bar_close: datetime, outcome: str) -> None:
        self.done.setdefault((subscription_id, bar_close), outcome)

    def orders_for_bar(self, subscription_id: str, bar_close: datetime, coin: str) -> Sequence[OrderRecord]:
        return sorted((o for o in self.orders.values() if o.subscription_id == subscription_id
                       and o.bar_close == bar_close and o.coin == coin), key=lambda o: o.attempt)

    def get_order(self, cloid: str) -> OrderRecord | None:
        return self.orders.get(cloid)

    def get_subscription(self, subscription_id: str) -> SubscriptionView | None:
        s = self.subs.get(subscription_id)
        return self._view(s) if s is not None else None

    def closing_subscriptions(self, limit: int) -> Sequence[SubscriptionView]:
        return [self._view(s) for s in self.subs.values() if s.status == "closing"][:limit]

    def unresolved_orders(self, subscription_id: str) -> Sequence[OrderRecord]:
        return sorted((o for o in self.orders.values() if o.subscription_id == subscription_id
                       and o.status in ("submitting", "unknown", "resting")), key=lambda o: (o.bar_close, o.attempt))

    def finish_closing(self, subscription_id: str, now: datetime) -> bool:
        s = self.subs.get(subscription_id)
        if s is None or s.status != "closing":
            return False
        self.subs[subscription_id] = replace(s, status="cancelled")
        return True

    def insert_order(self, record: OrderRecord) -> bool:
        if record.cloid in self.orders:
            return False
        self.orders[record.cloid] = record
        return True

    def update_order(self, cloid: str, *, status: str, filled_sz: Decimal, avg_px: Decimal | None, oid: int | None,
                     error: str | None, hl_response: Mapping[str, Any]) -> None:
        self.orders[cloid] = replace(self.orders[cloid], status=status, filled_sz=filled_sz, avg_px=avg_px, oid=oid,
                                     error=error, hl_response=dict(hl_response))

    def record_rejection(self, subscription_id: str, reason: str) -> int:
        self.rejections[subscription_id] = self.rejections.get(subscription_id, 0) + 1
        return self.rejections[subscription_id]

    def reset_rejections(self, subscription_id: str) -> None:
        self.rejections[subscription_id] = 0

    def record_target(self, subscription_id: str, coin: str, bar_close: datetime, target_notional_micro: int,
                      weight_bps: int) -> None:
        self.targets[(subscription_id, coin)] = (bar_close, target_notional_micro, weight_bps)

    def orders_of(self, subscription_id: str) -> list[OrderRecord]:
        return sorted((o for o in self.orders.values() if o.subscription_id == subscription_id),
                      key=lambda o: (o.coin, o.attempt))


class FakeMarket:
    def __init__(self, clock: FakeClock, prices: Mapping[str, str] | None = None) -> None:
        self.clock = clock
        self.prices = {SILVER: Decimal("30"), "BTC": Decimal("60000"), **{k: Decimal(v) for k, v in (prices or {}).items()}}
        self.sz_decimals = {SILVER: 2, "BTC": 5}
        self.calls = 0
        self.stale = False

    def snapshot(self, coin: str) -> MarketSnapshot | None:
        self.calls += 1
        if coin not in self.prices:
            return None
        px = self.prices[coin]
        as_of = self.clock.now() - (timedelta(seconds=600) if self.stale else timedelta(seconds=1))
        return MarketSnapshot(coin=coin, mid_px=px, mark_px=px, oracle_px=px,
                              day_notional_volume_micro=usd(1_000_000_000), open_interest_notional_micro=usd(500_000_000),
                              max_leverage=10, sz_decimals=self.sz_decimals.get(coin, 2), as_of=as_of)


class FakeExchange:
    """PositionReader + OrderStatusReader + GatewayFactory backed by one in-memory book.

    ``script[coin]`` is a list of behaviours consumed per order: "fill" (default), "partial:<ratio>", "reject",
    "raise_after" (exchange accepts & fills, then the client times out), "raise_before" (never reaches exchange)."""

    def __init__(self, market: FakeMarket) -> None:
        self.market = market
        self.pos: dict[tuple[str, str], Decimal] = {}
        self.placed: list[dict[str, Any]] = []            # orders the exchange actually accepted
        self.by_cloid: dict[str, PlaceResult] = {}
        self.script: dict[str, list[str]] = {}
        self.fail_positions_for: set[str] = set()
        self.position_error: Exception | None = None
        self.created: list[tuple[Any, str, str | None]] = []
        self.oid = 0

    # PositionReader
    def positions(self, address: str, coins: Iterable[str]) -> Mapping[str, Position]:
        if address in self.fail_positions_for:
            raise self.position_error or RuntimeError("boom")
        out = {}
        for c in coins:
            szi = self.pos.get((address, c), Decimal(0))
            out[c] = Position(coin=c, szi=szi, notional_micro=int(szi * self.market.prices[c] * MICRO))
        return out

    def set_position(self, address: str, coin: str, szi: str) -> None:
        self.pos[(address, coin)] = Decimal(szi)

    # OrderStatusReader
    def order_status_by_cloid(self, address: str, cloid: str) -> PlaceResult | None:
        return self.by_cloid.get(cloid)

    # GatewayFactory
    def create(self, key: Any, account_address: str, vault_address: str | None) -> "FakeGateway":
        assert isinstance(key, bytearray) and any(key), "gateway must receive the live (non-zeroized) key"
        self.created.append((key, account_address, vault_address))
        return FakeGateway(self, vault_address or account_address)


class FakeGateway:
    def __init__(self, ex: FakeExchange, address: str) -> None:
        self.ex = ex
        self.address = address

    def place_ioc(self, *, coin: str, is_buy: bool, sz: Decimal, limit_px: Decimal, reduce_only: bool,
                  cloid: str) -> PlaceResult:
        ex = self.ex
        queue = ex.script.get(coin)
        behaviour = queue.pop(0) if queue else "fill"
        if behaviour == "raise_before":
            raise TimeoutError("connect timeout")
        if cloid in ex.by_cloid:
            raise AssertionError(f"duplicate cloid sent to exchange: {cloid}")
        if behaviour == "reject":
            res = PlaceResult(status="rejected", error="Insufficient margin")
            ex.by_cloid[cloid] = res
            return res
        ratio = Decimal(behaviour.split(":")[1]) if behaviour.startswith("partial") else Decimal(1)
        filled = (sz * ratio).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        key = (self.address, coin)
        cur = ex.pos.get(key, Decimal(0))
        if reduce_only:
            filled = min(filled, abs(cur))
        ex.pos[key] = cur + (filled if is_buy else -filled)
        ex.oid += 1
        res = PlaceResult(status="filled" if filled == sz else "partial", filled_sz=filled,
                          avg_px=ex.market.prices[coin], oid=ex.oid, raw={"oid": ex.oid})
        ex.by_cloid[cloid] = res
        ex.placed.append({"coin": coin, "is_buy": is_buy, "sz": sz, "filled": filled, "reduce_only": reduce_only,
                          "cloid": cloid, "limit_px": limit_px, "address": self.address})
        if behaviour == "raise_after":
            raise TimeoutError("read timeout")
        return res


class FakeKeys:
    def __init__(self) -> None:
        self.opened = 0
        self.last: bytearray | None = None
        self.open_now = 0

    @contextmanager
    def agent_key(self, user_id: str, master_address: str):
        key = bytearray(b"\x11" * 32)
        self.opened += 1
        self.open_now += 1
        self.last = key
        try:
            yield key
        finally:
            for i in range(len(key)):
                key[i] = 0
            self.open_now -= 1


class FakeLocks:
    def __init__(self) -> None:
        self.held: set[str] = set()

    @contextmanager
    def try_lock(self, key: str):
        if key in self.held:
            yield False
            return
        self.held.add(key)
        try:
            yield True
        finally:
            self.held.discard(key)


class FakeFlags:
    def __init__(self) -> None:
        self.value = Flags()

    def flags(self) -> Flags:
        return self.value


class FakeAlerts:
    def __init__(self) -> None:
        self.items: list[AlertEvent] = []

    def emit(self, alert: AlertEvent) -> None:
        self.items.append(alert)

    def kinds(self) -> list[str]:
        return [a.kind for a in self.items]


class FakeJitter:
    def __init__(self, delays: Mapping[str, int] | None = None, order: Sequence[str] | None = None) -> None:
        self.delays = dict(delays or {})
        self.order = list(order) if order else None

    def delay_seconds(self, user_id: str, bar_close: datetime) -> int:
        return self.delays.get(user_id, 0)

    def fair_order(self, subscription_ids: Sequence[str], bar_close: datetime) -> list[str]:
        ids = sorted(subscription_ids)
        if self.order:
            ids = [i for i in self.order if i in ids] + [i for i in ids if i not in self.order]
        return ids


def make_sub(i: int = 1, **kw: Any) -> SubscriptionView:
    base = dict(id=f"sub{i}", user_id=f"user{i}", strategy_id="strat-silver", strategy_version_id="v1",
                master_address=f"0xmaster{i}", trading_address=f"0xmaster{i}", allocation_micro=usd(10_000),
                max_leverage_x100=300, status="active", markets=(SILVER,))
    base.update(kw)
    return SubscriptionView(**base)


def make_signal(weight: int = 1, coin: str = SILVER, bar: datetime = BAR, version: str = "v1") -> BarSignal:
    return BarSignal(strategy_version_id=version, bar_close=bar, weights_bps={coin: weight * 10_000})


class World:
    """One executor with all fakes wired (real domain RiskPlanner by default)."""

    def __init__(self, subs: Sequence[SubscriptionView] = (), signals: Sequence[BarSignal] = (), planner: Any = None,
                 jitter: Any = None, **cfg: Any) -> None:
        from app.execution.executor import Executor, ExecutorConfig
        from app.execution.wiring import RiskPlanner

        self.clock = FakeClock(BAR + timedelta(minutes=11))
        self.signals = FakeSignals(signals or [make_signal()])
        self.subs = FakeSubRepo(subs or [make_sub()])
        self.market = FakeMarket(self.clock)
        self.ex = FakeExchange(self.market)
        self.keys = FakeKeys()
        self.locks = FakeLocks()
        self.flags = FakeFlags()
        self.alerts = FakeAlerts()
        self.jitter = jitter or FakeJitter()
        self.executor = Executor(
            signals=self.signals, subscriptions=self.subs, market_data=self.market, positions=self.ex,
            order_status=self.ex, keys=self.keys, gateways=self.ex, flags=self.flags, alerts=self.alerts,
            clock=self.clock, locks=self.locks, planner=planner or RiskPlanner(), jitter=self.jitter,
            config=ExecutorConfig(**cfg))

    def tick(self, advance_seconds: int = 60):
        self.clock.set(self.clock.now() + timedelta(seconds=advance_seconds))
        return self.executor.run_tick()


# ----------------------------------------------------------------------------------------------- settlement fakes

class FakeLedger:
    def __init__(self) -> None:
        self.txs: dict[str, tuple[str, str, tuple[LedgerLine, ...]]] = {}
        self.balances: dict[str, int] = {}
        self.posts = 0

    def post_transaction(self, *, idempotency_key: str, kind: str, memo: str, lines: Sequence[LedgerLine],
                         created_by: str) -> tuple[str, bool]:
        self.posts += 1
        if sum(ln.amount_micro for ln in lines) != 0:
            raise AssertionError("unbalanced")
        if idempotency_key in self.txs:
            tx_id, _, old = self.txs[idempotency_key]
            if tuple(old) != tuple(lines):
                raise Conflict("idempotency key reused with different lines")
            return tx_id, False
        tx_id = f"tx{len(self.txs) + 1}"
        self.txs[idempotency_key] = (tx_id, kind, tuple(lines))
        for ln in lines:
            self.balances[ln.account_code] = self.balances.get(ln.account_code, 0) + ln.amount_micro
        return tx_id, True

    def has_transaction(self, idempotency_key: str) -> bool:
        return idempotency_key in self.txs

    def balance(self, account_code: str) -> int:
        return self.balances.get(account_code, 0)

    def top_up(self, user_id: str, amount: int) -> None:
        key = f"dep:{user_id}:{len(self.txs)}"
        self.post_transaction(idempotency_key=key, kind="deposit", memo="", created_by="t", lines=[
            LedgerLine("treasury:hl_usdc", amount), LedgerLine(f"user:{user_id}:fee_balance", -amount)])

    def available(self, user_id: str) -> int:
        return -self.balance(f"user:{user_id}:fee_balance")


class FakeUow:
    def __init__(self) -> None:
        self.depth = 0
        self.commits = 0

    @contextmanager
    def atomic(self):
        self.depth += 1
        try:
            yield
            self.commits += 1
        finally:
            self.depth -= 1


class FakeSettlementRepo:
    def __init__(self) -> None:
        self.subs: dict[str, SettlementSubscription] = {}
        self.pnl: dict[str, list[tuple[datetime, int]]] = {}     # sub -> [(time, micro)]
        self.settled: set[tuple[str, date]] = set()
        self.fills: dict[str, BuilderFeeFill] = {}
        self.recognised: dict[tuple[str, str], str] = {}   # (trading_address, tid) -> ledger tx id
        self.plans: dict[str, PlanAccount] = {}

    def add_pnl(self, sub_id: str, when: datetime, micro: int) -> None:
        self.pnl.setdefault(sub_id, []).append((when, micro))

    def subscriptions_to_settle(self) -> Sequence[SettlementSubscription]:
        return list(self.subs.values())

    def is_settled(self, subscription_id: str, settle_date: date) -> bool:
        return (subscription_id, settle_date) in self.settled

    def pnl_since(self, subscription_id: str, since: datetime | None, until: datetime) -> PnlDelta:
        total = sum(m for t, m in self.pnl.get(subscription_id, []) if (since is None or t > since) and t <= until)
        return PnlDelta(realized_micro=total, funding_micro=0, until=until)

    def save_profit_share(self, subscription_id: str, settle_date: date, *, cum_pnl_micro: int, hwm_micro: int,
                          pnl_cursor: datetime, ledger_tx_id: str | None) -> None:
        self.settled.add((subscription_id, settle_date))
        self.subs[subscription_id] = replace(self.subs[subscription_id], cum_pnl_micro=cum_pnl_micro,
                                             hwm_micro=hwm_micro, pnl_cursor=pnl_cursor)

    def set_status(self, subscription_id: str, status: str, past_due_since: datetime | None) -> None:
        self.subs[subscription_id] = replace(self.subs[subscription_id], status=status, past_due_since=past_due_since)

    def set_period_end(self, subscription_id: str, period_end: datetime) -> None:
        self.subs[subscription_id] = replace(self.subs[subscription_id], current_period_end=period_end)

    def unrecognised_builder_fee_fills(self, until: datetime, limit: int) -> Sequence[BuilderFeeFill]:
        return [f for t, f in sorted(self.fills.items())
                if (f.trading_address, f.tid) not in self.recognised and f.time <= until][:limit]

    def mark_builder_fee_recognised(self, trading_address: str, tid: str, ledger_tx_id: str) -> None:
        self.recognised[(trading_address, tid)] = ledger_tx_id

    def plans_due(self, now: datetime) -> Sequence[PlanAccount]:
        return [p for p in self.plans.values() if p.plan != "free" and p.plan_period_end and p.plan_period_end <= now]

    def set_plan_period(self, user_id: str, plan_period_end: datetime | None, past_due_since: datetime | None) -> None:
        self.plans[user_id] = replace(self.plans[user_id], plan_period_end=plan_period_end, past_due_since=past_due_since)

    def downgrade_plan(self, user_id: str, plan: str) -> None:
        self.plans[user_id] = replace(self.plans[user_id], plan=plan, price_monthly_micro=0)


class FakeReferrals:
    def __init__(self, mapping: Mapping[str, tuple[str, int]] | None = None) -> None:
        self.mapping = dict(mapping or {})

    def referrer_share(self, user_id: str) -> tuple[str, int] | None:
        return self.mapping.get(user_id)


# ----------------------------------------------------------------------------------------------- reconcile fakes

class FakeReconcileRepo:
    def __init__(self, expected: Sequence[ExpectedPosition] = (), builder_total: int = 0) -> None:
        self.expected = list(expected)
        self.builder_total = builder_total

    def expected_positions(self) -> Sequence[ExpectedPosition]:
        return list(self.expected)

    def total_builder_fees_micro(self) -> int:
        return self.builder_total


class Const:
    def __init__(self, value: int) -> None:
        self.value = value

    def cumulative_builder_rewards_micro(self) -> int:
        return self.value

    def treasury_usdc_micro(self) -> int:
        return self.value


class FakesSanityTest(unittest.TestCase):
    def test_ledger_rejects_unbalanced_and_is_idempotent(self):
        led = FakeLedger()
        with self.assertRaises(AssertionError):
            led.post_transaction(idempotency_key="k", kind="x", memo="", created_by="t",
                                 lines=[LedgerLine("a", 1), LedgerLine("b", -2)])
        led.top_up("u", usd(10))
        self.assertEqual(led.available("u"), usd(10))

    def test_gateway_fill_moves_position(self):
        clock = FakeClock()
        ex = FakeExchange(FakeMarket(clock))
        gw = ex.create(bytearray(b"\x01" * 32), "0xa", None)
        gw.place_ioc(coin=SILVER, is_buy=True, sz=Decimal("10"), limit_px=Decimal("31"), reduce_only=False, cloid="0x1")
        self.assertEqual(ex.positions("0xa", [SILVER])[SILVER].szi, Decimal("10"))


if __name__ == "__main__":
    unittest.main()
