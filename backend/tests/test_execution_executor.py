"""Executor tick tests (stdlib unittest; in-memory fakes + the real domain risk planner)."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import BAR, SILVER, TRUSTED_DEXES, FakeJitter, World, make_signal, make_sub, usd  # noqa: E402

from app.errors import ExternalServiceError  # noqa: E402
from app.execution.executor import CLOID_PREFIX, is_our_cloid, make_cloid  # noqa: E402
from app.execution.ports import (  # noqa: E402
    ORDER_FILLED,
    ORDER_NOT_SUBMITTED,
    ORDER_PARTIAL,
    ORDER_SUBMITTING,
    Flags,
    OrderRecord,
)


class CloidTest(unittest.TestCase):
    def test_deterministic_prefixed_128_bit(self):
        a = make_cloid("sub1", BAR, SILVER, 0)
        self.assertEqual(a, make_cloid("sub1", BAR, SILVER, 0))
        self.assertNotEqual(a, make_cloid("sub1", BAR, SILVER, 1))
        self.assertNotEqual(a, make_cloid("sub2", BAR, SILVER, 0))
        self.assertEqual(len(a), 34)
        self.assertTrue(a.startswith("0x" + CLOID_PREFIX))
        int(a, 16)
        self.assertTrue(is_our_cloid(a))
        self.assertFalse(is_our_cloid("0x" + "0" * 32))


class HappyPathTest(unittest.TestCase):
    def test_places_one_order_records_everything_and_is_done(self):
        w = World()
        r = w.tick()
        self.assertEqual(len(w.ex.placed), 1)
        o = w.ex.placed[0]
        self.assertTrue(o["is_buy"])
        self.assertFalse(o["reduce_only"])
        self.assertEqual(o["sz"], Decimal("333.33"))            # $10k / $30, floored to szDecimals=2
        self.assertEqual(o["limit_px"], Decimal("30.15"))       # mid + 0.5% slippage cap
        self.assertEqual(o["cloid"], make_cloid("sub1", BAR, SILVER, 0))
        rec = w.subs.get_order(o["cloid"])
        self.assertEqual(rec.status, ORDER_FILLED)
        self.assertEqual(rec.filled_sz, Decimal("333.33"))
        self.assertEqual(rec.strategy_version_id, "v1")
        self.assertEqual(w.subs.done[("sub1", BAR)], "ok")
        self.assertEqual(w.subs.targets[("sub1", SILVER)][1], usd(10_000))
        self.assertEqual((r.orders_placed, r.orders_filled, r.bars_completed, r.errors), (1, 1, 1, 0))
        # key opened once, zeroized after use, gateway got no vault for a master-account subscription
        self.assertEqual(w.keys.opened, 1)
        self.assertEqual(w.keys.open_now, 0)
        self.assertFalse(any(w.keys.last))
        self.assertEqual(w.ex.created[0][1:], ("0xmaster1", None))

        r2 = w.tick()
        self.assertEqual(len(w.ex.placed), 1)
        self.assertEqual(r2.due, 0)

    def test_no_key_opened_when_nothing_to_do(self):
        w = World(signals=[make_signal(weight=0)])
        w.tick()
        self.assertEqual(w.ex.placed, [])
        self.assertEqual(w.keys.opened, 0)
        self.assertIn(("sub1", BAR), w.subs.done)

    def test_sub_account_uses_vault_address(self):
        w = World(subs=[make_sub(1, trading_address="0xsubacct")])
        w.tick()
        self.assertEqual(w.ex.created[0][1:], ("0xmaster1", "0xsubacct"))
        self.assertEqual(w.ex.placed[0]["address"], "0xsubacct")

    def test_logs_contain_no_key_material(self):
        w = World()
        with self.assertLogs("app.execution", level="INFO") as cm:
            w.tick()
        blob = "\n".join(cm.output) + json.dumps([getattr(r, "fields", {}) for r in cm.records], default=str)
        self.assertNotIn("11" * 32, blob)
        self.assertNotIn("0xmaster1", blob)  # wallet addresses are not logged either


class IdempotencyTest(unittest.TestCase):
    def test_timeout_after_exchange_accepted_is_resolved_not_resent(self):
        w = World()
        w.ex.script[SILVER] = ["raise_after"]
        r1 = w.tick()
        self.assertEqual(r1.orders_unknown, 1)
        self.assertEqual(len(w.ex.placed), 1)
        self.assertNotIn(("sub1", BAR), w.subs.done)
        r2 = w.tick()
        self.assertEqual(len(w.ex.placed), 1, "must never place a second order")
        self.assertEqual(w.subs.get_order(make_cloid("sub1", BAR, SILVER, 0)).status, ORDER_FILLED)
        self.assertIn(("sub1", BAR), w.subs.done)
        self.assertEqual(r2.orders_placed, 0)

    def test_never_reached_exchange_is_retried_once_after_grace(self):
        w = World(unknown_order_grace_seconds=120)
        w.ex.script[SILVER] = ["raise_before"]
        w.tick()
        self.assertEqual(w.ex.placed, [])
        w.tick()                                  # 60s later: may still be in flight → wait
        self.assertEqual(w.ex.placed, [])
        self.assertEqual(w.subs.get_order(make_cloid("sub1", BAR, SILVER, 0)).status, "unknown")
        w.tick()                                  # 120s: exchange never saw it → not_submitted → new attempt
        self.assertEqual(w.subs.get_order(make_cloid("sub1", BAR, SILVER, 0)).status, ORDER_NOT_SUBMITTED)
        self.assertEqual(len(w.ex.placed), 1)
        self.assertEqual(w.ex.placed[0]["cloid"], make_cloid("sub1", BAR, SILVER, 1))
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)

    def test_existing_intent_row_blocks_placement(self):
        w = World()
        cloid = make_cloid("sub1", BAR, SILVER, 0)
        w.subs.insert_order(OrderRecord(
            subscription_id="sub1", strategy_version_id="v1", bar_close=BAR, attempt=0, cloid=cloid, coin=SILVER,
            is_buy=True, sz=Decimal("333.33"), limit_px=Decimal("30.15"), reduce_only=False, status=ORDER_SUBMITTING,
            jitter_seconds=0, submitted_at=w.clock.now()))
        r = w.tick()
        self.assertEqual(w.ex.placed, [])
        self.assertEqual(r.unresolved_orders, 1)

    def test_concurrent_tick_holding_lock_is_skipped(self):
        w = World()
        w.locks.held.add("exec:sub:sub1")
        r = w.tick()
        self.assertEqual((r.locked, len(w.ex.placed)), (1, 0))
        w.locks.held.clear()
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)


class JitterTest(unittest.TestCase):
    def test_waits_until_bar_close_plus_delay(self):
        w = World(jitter=FakeJitter({"user1": 900}))
        w.clock.set(BAR + timedelta(seconds=839))
        r = w.tick()                               # t = 899s
        self.assertEqual((r.not_due, len(w.ex.placed)), (1, 0))
        w.tick(advance_seconds=1)                  # t = 900s
        self.assertEqual(len(w.ex.placed), 1)
        self.assertEqual(w.subs.get_order(w.ex.placed[0]["cloid"]).jitter_seconds, 900)

    def test_domain_jitter_is_deterministic_and_bounded(self):
        from app.execution.wiring import DomainJitter
        j = DomainJitter(b"s" * 32, 600)
        d = j.delay_seconds("user1", BAR)
        self.assertEqual(d, j.delay_seconds("user1", BAR))
        self.assertTrue(0 <= d <= 600)
        ids = [f"sub{i}" for i in range(20)]
        self.assertEqual(j.fair_order(ids, BAR), j.fair_order(list(reversed(ids)), BAR))
        self.assertEqual(sorted(j.fair_order(ids, BAR)), sorted(ids))

    def test_fair_order_decides_processing_order(self):
        subs = [make_sub(1), make_sub(2)]
        w = World(subs=subs, jitter=FakeJitter(order=["sub2", "sub1"]), max_subscriptions_per_tick=1)
        w.tick()
        self.assertEqual(w.ex.placed[0]["cloid"], make_cloid("sub2", BAR, SILVER, 0))


class KillSwitchTest(unittest.TestCase):
    def test_global_kill_switch_places_nothing(self):
        w = World()
        w.flags.value = Flags(trusted_dexes=TRUSTED_DEXES, kill_switch_global=True)
        r = w.tick()
        self.assertTrue(r.kill_switch_global)
        self.assertEqual((w.ex.placed, w.keys.opened), ([], 0))

    def test_market_kill_switch_blocks_coin_until_lifted(self):
        w = World()
        w.flags.value = Flags(trusted_dexes=TRUSTED_DEXES, killed_markets=frozenset({SILVER}))
        r = w.tick()
        self.assertEqual((r.market_killed, len(w.ex.placed)), (1, 0))
        self.assertNotIn(("sub1", BAR), w.subs.done)
        w.flags.value = Flags(trusted_dexes=TRUSTED_DEXES)
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)

    def test_new_entries_paused_allows_exit_only(self):
        w = World(signals=[make_signal(weight=2)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.flags.value = Flags(trusted_dexes=TRUSTED_DEXES, new_entries_paused=True)
        w.tick()
        self.assertEqual(w.ex.placed, [])
        self.assertIn(("sub1", BAR), w.subs.done)


class ReduceOnlyTest(unittest.TestCase):
    def test_reduce_only_status_never_increases(self):
        w = World(subs=[make_sub(1, status="reduce_only")], signals=[make_signal(weight=2)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.tick()
        self.assertEqual(w.ex.placed, [])
        self.assertEqual(w.subs.targets[("sub1", SILVER)][1], usd(3000))   # hold current exposure

    def test_reduce_only_status_still_exits(self):
        w = World(subs=[make_sub(1, status="reduce_only")], signals=[make_signal(weight=0)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)
        o = w.ex.placed[0]
        self.assertEqual((o["is_buy"], o["reduce_only"], o["sz"]), (False, True, Decimal("100")))
        self.assertEqual(w.ex.pos[("0xmaster1", SILVER)], Decimal(0))

    def test_reduce_only_flat_opens_nothing(self):
        w = World(subs=[make_sub(1, status="reduce_only")], signals=[make_signal(weight=1)])
        w.tick()
        self.assertEqual(w.ex.placed, [])

    def test_past_due_within_grace_still_trades(self):
        w = World()
        w.subs.add(make_sub(1, status="past_due", past_due_since=BAR - timedelta(hours=1)))
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)

    def test_past_due_after_grace_is_exits_only(self):
        w = World()
        w.subs.add(make_sub(1, status="past_due", past_due_since=BAR - timedelta(hours=80)))
        w.tick()
        self.assertEqual(w.ex.placed, [])

    # SPEC §12 alert-contacts entries gate: entries_allowed=False ⇒ reduce-only for the tick (exits still run)
    def test_contacts_gate_opens_nothing_from_flat(self):
        w = World(subs=[make_sub(1, entries_allowed=False)], signals=[make_signal(weight=1)])
        r = w.tick()
        self.assertEqual(w.ex.placed, [])
        self.assertEqual(r.contacts_gated, 1)
        self.assertEqual(w.subs.done[("sub1", BAR)], "ok")          # nothing to do this bar (not an error)

    def test_contacts_gate_never_increases(self):
        w = World(subs=[make_sub(1, entries_allowed=False)], signals=[make_signal(weight=2)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.tick()
        self.assertEqual(w.ex.placed, [])
        self.assertEqual(w.subs.targets[("sub1", SILVER)][1], usd(3000))   # holds current exposure

    def test_contacts_gate_still_exits(self):
        w = World(subs=[make_sub(1, entries_allowed=False)], signals=[make_signal(weight=0)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)
        o = w.ex.placed[0]
        self.assertEqual((o["is_buy"], o["reduce_only"], o["sz"]), (False, True, Decimal("100")))
        self.assertEqual(w.ex.pos[("0xmaster1", SILVER)], Decimal(0))

    def test_contacts_gate_in_planner_alone_blocks_entries(self):
        """The planner treats entries_allowed=False as reduce_only even without the executor's reduce_only_mode."""
        from app.execution.ports import MarketSnapshot, PlanInput, Position
        from app.execution.wiring import RiskPlanner

        w = World()
        snap = w.market.snapshot(SILVER)
        planner = RiskPlanner()
        for allowed, expect_legs in ((True, 1), (False, 0)):
            sub = make_sub(1, entries_allowed=allowed)
            try:
                plan = planner.plan(PlanInput(subscription=sub, coin=SILVER, weight_bps=10_000,
                                              position=Position.flat(SILVER), snapshot=snap, flags=Flags(trusted_dexes=TRUSTED_DEXES),
                                              reduce_only_mode=False, now=w.clock.now()))
                legs = len(plan.legs)
            except Exception:  # noqa: BLE001 - a guard rejection also means "nothing opened"
                legs = 0
            self.assertEqual(legs, expect_legs, allowed)
        self.assertIsInstance(snap, MarketSnapshot)

    def test_paused_and_cancelled_are_not_traded(self):
        for st in ("paused_user", "cancelled", "pending"):
            w = World(subs=[make_sub(1, status=st)])
            w.tick()
            self.assertEqual(w.ex.placed, [], st)


class PartialFillTest(unittest.TestCase):
    def test_residual_is_placed_next_tick_with_new_cloid(self):
        w = World()
        w.ex.script[SILVER] = ["partial:0.5"]
        r1 = w.tick()
        self.assertEqual(r1.orders_partial, 1)
        self.assertEqual(w.subs.get_order(make_cloid("sub1", BAR, SILVER, 0)).status, ORDER_PARTIAL)
        self.assertNotIn(("sub1", BAR), w.subs.done)
        w.tick()
        self.assertEqual(len(w.ex.placed), 2)
        second = w.ex.placed[1]
        self.assertEqual(second["cloid"], make_cloid("sub1", BAR, SILVER, 1))
        self.assertEqual(second["sz"], Decimal("166.67"))
        self.assertEqual(w.ex.pos[("0xmaster1", SILVER)], Decimal("333.33"))
        self.assertEqual(w.subs.done[("sub1", BAR)], "ok")

    def test_gives_up_after_max_attempts_and_alerts(self):
        w = World(max_attempts_per_bar=3)
        w.ex.script[SILVER] = ["partial:0.1"] * 10
        for _ in range(5):
            w.tick()
        self.assertEqual(len(w.ex.placed), 3)
        self.assertEqual(w.subs.done[("sub1", BAR)], "residual")
        self.assertIn("execution_residual", w.alerts.kinds())

    def test_flip_places_close_then_open(self):
        w = World(signals=[make_signal(weight=-1)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.tick()
        self.assertEqual(len(w.ex.placed), 2)
        close, open_ = w.ex.placed
        self.assertEqual((close["is_buy"], close["reduce_only"], close["sz"]), (False, True, Decimal("100")))
        self.assertEqual((open_["is_buy"], open_["reduce_only"]), (False, False))
        self.assertEqual(w.ex.pos[("0xmaster1", SILVER)], Decimal("-333.33"))

    def test_flip_open_leg_waits_for_complete_close(self):
        w = World(signals=[make_signal(weight=-1)])
        w.ex.set_position("0xmaster1", SILVER, "100")
        w.ex.script[SILVER] = ["partial:0.5"]
        w.tick()
        self.assertEqual(len(w.ex.placed), 1)            # open leg not sent after a partial close


class IsolationAndBreakerTest(unittest.TestCase):
    def test_one_subscription_failing_does_not_stop_others(self):
        w = World(subs=[make_sub(1), make_sub(2)])
        w.ex.fail_positions_for.add("0xmaster1")
        r = w.tick()
        self.assertEqual(r.errors, 1)
        self.assertEqual(r.error_subscriptions, ["sub1"])
        self.assertEqual([o["address"] for o in w.ex.placed], ["0xmaster2"])
        self.assertEqual(w.subs.rejections.get("sub1"), 1)     # unexpected exception counts towards the breaker
        self.assertIn("execution_error", w.alerts.kinds())

    def test_external_outage_does_not_count_towards_breaker(self):
        w = World()
        w.ex.fail_positions_for.add("0xmaster1")
        w.ex.position_error = ExternalServiceError("hl info down")
        w.tick()
        self.assertEqual(w.subs.rejections.get("sub1", 0), 0)

    def test_alert_sink_failure_never_breaks_execution(self):
        w = World(subs=[make_sub(1), make_sub(2)])
        w.ex.fail_positions_for.add("0xmaster1")

        def boom(_a):
            raise RuntimeError("sink down")
        w.alerts.emit = boom
        r = w.tick()
        self.assertEqual(len(w.ex.placed), 1)
        self.assertEqual(r.errors, 1)

    def test_circuit_breaker_after_three_exchange_rejections(self):
        w = World()
        w.ex.script[SILVER] = ["reject"] * 10
        for _ in range(3):
            w.tick()
        self.assertEqual(w.subs.rejections["sub1"], 3)
        self.assertIn("order_rejections_burst", w.alerts.kinds())
        sent = len(w.ex.by_cloid)
        r = w.tick()
        self.assertEqual(r.breaker_open, 1)
        self.assertEqual(len(w.ex.by_cloid), sent)

    def test_fill_resets_rejection_counter(self):
        w = World()
        w.ex.script[SILVER] = ["reject"]
        w.tick()
        self.assertEqual(w.subs.rejections["sub1"], 1)
        w.tick()
        self.assertEqual(w.subs.rejections["sub1"], 0)

    def test_guard_rejection_does_not_trip_breaker(self):
        w = World()
        w.market.stale = True
        for _ in range(5):
            r = w.tick()
        self.assertEqual(w.subs.rejections.get("sub1", 0), 0)
        self.assertEqual(r.market_rejections, 1)
        self.assertEqual(w.ex.placed, [])


class SignalAndBudgetTest(unittest.TestCase):
    def test_signal_coin_outside_whitelist_is_never_traded(self):
        w = World(signals=[make_signal(coin="BTC")])
        w.tick()
        self.assertEqual(w.ex.placed, [])
        crit = [a for a in w.alerts.items if a.kind == "signal_market_not_whitelisted"]
        self.assertEqual(crit[0].severity, "critical")
        self.assertIsNone(crit[0].coin)

    def test_stale_signal_skipped_with_alert(self):
        w = World(signals=[make_signal(bar=BAR - timedelta(hours=40))])
        r = w.tick()
        self.assertEqual((r.stale_signals, len(w.ex.placed)), (1, 0))
        self.assertIn("stale_signal", w.alerts.kinds())

    def test_max_subscriptions_per_tick(self):
        w = World(subs=[make_sub(1), make_sub(2)], max_subscriptions_per_tick=1)
        r = w.tick()
        self.assertEqual((r.processed, r.deferred, len(w.ex.placed)), (1, 1, 1))
        w.tick()
        self.assertEqual(len(w.ex.placed), 2)

    def test_time_budget(self):
        w = World(subs=[make_sub(1), make_sub(2)], time_budget_seconds=45)
        w.clock.mono_step = 30
        r = w.tick()
        self.assertEqual((r.processed, r.deferred), (1, 1))

    def test_versions_are_interleaved(self):
        subs = [make_sub(1), make_sub(2), make_sub(3, strategy_version_id="v2")]
        w = World(subs=subs, signals=[make_signal(version="v1"), make_signal(version="v2")],
                  max_subscriptions_per_tick=2)
        w.tick()
        addrs = sorted(o["address"] for o in w.ex.placed)
        self.assertEqual(addrs, ["0xmaster1", "0xmaster3"])

    def test_naive_now_rejected(self):
        w = World()
        with self.assertRaises(ValueError):
            w.executor.run_tick(BAR.replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
