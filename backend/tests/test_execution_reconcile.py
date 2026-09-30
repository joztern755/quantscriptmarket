"""Reconciliation, in-house registry and domain-adapter tests."""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_execution_fakes import (  # noqa: E402
    BAR,
    SILVER,
    TRUSTED_DEXES,
    Const,
    FakeAlerts,
    FakeClock,
    FakeExchange,
    FakeLedger,
    FakeMarket,
    FakeReconcileRepo,
    make_sub,
    usd,
)

from app.errors import GuardRejected, ValidationFailed  # noqa: E402
from app.execution.ports import AlertEvent, ExpectedPosition, Flags, PlanInput, Position  # noqa: E402
from app.execution.reconcile import Reconciler  # noqa: E402
from app.execution.wiring import NotifierAlertSink, RiskPlanner  # noqa: E402
from app.strategies import registry  # noqa: E402


def expected(target: int, sub_id: str = "sub1", addr: str = "0xmaster1") -> ExpectedPosition:
    return ExpectedPosition(subscription_id=sub_id, user_id="user1", trading_address=addr, coin=SILVER,
                            target_notional_micro=target, allocation_micro=usd(10_000), bar_close=BAR)


class ReconcileTest(unittest.TestCase):
    def make(self, expected_list=(), builder_db=0, builder_chain=0, treasury_chain=0, treasury_ledger=0):
        clock = FakeClock()
        self.ex = FakeExchange(FakeMarket(clock))
        self.alerts = FakeAlerts()
        ledger = FakeLedger()
        if treasury_ledger:
            ledger.top_up("u", treasury_ledger)
        return Reconciler(repo=FakeReconcileRepo(expected_list, builder_db), positions=self.ex,
                          builder_rewards=Const(builder_chain), treasury=Const(treasury_chain), ledger=ledger,
                          alerts=self.alerts)

    def test_all_in_sync(self):
        rec = self.make([expected(usd(10_000))], builder_db=usd(10), builder_chain=usd(10),
                        treasury_chain=usd(100), treasury_ledger=usd(100))
        self.ex.set_position("0xmaster1", SILVER, "333.33")
        r = rec.run("2026-10-01")
        self.assertEqual((r.drifts, r.builder_mismatch, r.treasury_mismatch, r.errors), ([], False, False, []))
        self.assertEqual(r.positions_checked, 1)
        self.assertEqual(self.alerts.items, [])

    def test_position_drift_alert(self):
        rec = self.make([expected(usd(10_000))])
        r = rec.run("2026-10-01")                       # on-chain flat
        self.assertEqual(len(r.drifts), 1)
        self.assertEqual(r.drifts[0].drift_micro, -usd(10_000))
        self.assertEqual(self.alerts.kinds(), ["position_drift"])

    def test_small_price_drift_tolerated_sign_flip_not(self):
        rec = self.make([expected(usd(10_000))])
        self.ex.set_position("0xmaster1", SILVER, "310")   # $9,300: 7% of allocation < 10% tolerance
        self.assertEqual(rec.run("d").drifts, [])
        rec = self.make([expected(usd(10))])
        self.ex.set_position("0xmaster1", SILVER, "-0.2")  # tiny short vs tiny long target → flagged
        self.assertEqual(len(rec.run("d").drifts), 1)

    def test_builder_fee_mismatch_threshold_one_dollar(self):
        rec = self.make(builder_db=usd(10), builder_chain=usd(10) + 1_000_000)
        self.assertFalse(rec.run("d").builder_mismatch)   # exactly $1 → not above threshold
        rec = self.make(builder_db=usd(10), builder_chain=usd(10) + 1_000_001)
        r = rec.run("d")
        self.assertTrue(r.builder_mismatch)
        a = self.alerts.items[0]
        self.assertEqual((a.kind, a.severity, a.payload["scope"]), ("reconciliation_mismatch", "critical", "builder fees"))

    def test_treasury_mismatch(self):
        rec = self.make(treasury_chain=usd(98), treasury_ledger=usd(100))
        r = rec.run("d")
        self.assertTrue(r.treasury_mismatch)
        self.assertEqual(self.alerts.items[0].payload["diff_micro"], -usd(2))

    def test_failing_check_is_isolated(self):
        rec = self.make([expected(usd(10_000))], builder_db=0, builder_chain=usd(5))

        class Boom:
            def treasury_usdc_micro(self):
                raise RuntimeError("down")
        rec.treasury = Boom()
        r = rec.run("d")
        self.assertTrue(r.builder_mismatch)
        self.assertEqual(r.errors, ["treasury:RuntimeError"])
        self.assertIn("reconcile_check_failed", self.alerts.kinds())


class RegistryTest(unittest.TestCase):
    def test_silver_is_the_only_listed_strategy(self):
        self.assertEqual([s.key for s in registry.listed()], ["silver"])
        self.assertEqual(registry.status_for("silver"), "listed")
        for k in ("btc", "sol", "hype", "gold", "oil", "runners"):
            self.assertEqual(registry.status_for(k), "draft", k)
        self.assertEqual(registry.status_for("runners", ["runners"]), "draft")   # unmappable → never listed

    def test_silver_definition(self):
        s = registry.get("silver")
        self.assertEqual((s.markets, s.long_only, s.allowed_weights, s.source, s.timeframe),
                         (("xyz:SILVER",), True, (0, 1, 2), "terminal", "1d"))

    def test_market_mapping(self):
        self.assertEqual(registry.market_for_signal_key("silver"), "xyz:SILVER")
        self.assertEqual(registry.market_for_signal_key("BTC"), "BTC")
        self.assertEqual(registry.market_for_signal_key("gold"), "xyz:GOLD")
        self.assertEqual(registry.market_for_signal_key("oil"), "xyz:CL")
        with self.assertRaises(ValidationFailed):
            registry.market_for_signal_key("runners")
        with self.assertRaises(ValidationFailed):
            registry.get("doge")

    def test_terminal_weights(self):
        self.assertEqual(registry.terminal_weights_bps("silver", {"target_weight": 2, "market": "xyz:SILVER"}),
                         {"xyz:SILVER": 20_000})
        self.assertEqual(registry.terminal_weights_bps("silver", {"target_weight": 1.0}), {"xyz:SILVER": 10_000})
        for bad in ({"target_weight": 3}, {"target_weight": -1}, {"target_weight": 0.5}, {"target_weight": True},
                    {"target_weight": "1"}, {}, {"target_weight": 1, "market": "xyz:GOLD"}):
            with self.assertRaises(ValidationFailed, msg=str(bad)):
                registry.terminal_weights_bps("silver", bad)


class WiringTest(unittest.TestCase):
    def _inp(self, **kw):
        clock = FakeClock(BAR)
        market = FakeMarket(clock)
        base = dict(subscription=make_sub(), coin=SILVER, weight_bps=10_000, position=Position.flat(SILVER),
                    snapshot=market.snapshot(SILVER), flags=Flags(trusted_dexes=TRUSTED_DEXES), reduce_only_mode=False, now=BAR)
        base.update(kw)
        return PlanInput(**base), market

    def test_risk_planner_happy(self):
        inp, _ = self._inp()
        plan = RiskPlanner().plan(inp)
        self.assertEqual(plan.target_notional_micro, usd(10_000))
        self.assertEqual(len(plan.legs), 1)
        self.assertEqual(plan.legs[0].sz, Decimal("333.33"))

    def test_market_scope_rejection(self):
        inp, market = self._inp()
        market.stale = True
        inp = PlanInput(**{**inp.__dict__, "snapshot": market.snapshot(SILVER)})
        with self.assertRaises(GuardRejected) as cm:
            RiskPlanner().plan(inp)
        self.assertEqual(cm.exception.details["scope"], "market")
        self.assertIn("stale_data", cm.exception.details["reasons"])

    def test_subscription_scope_rejection(self):
        inp, _ = self._inp(subscription=make_sub(markets=("BTC",)))
        with self.assertRaises(GuardRejected) as cm:
            RiskPlanner().plan(inp)
        self.assertEqual(cm.exception.details["scope"], "subscription")

    def test_reduce_only_mode_blocks_entry(self):
        inp, _ = self._inp(reduce_only_mode=True)
        self.assertEqual(RiskPlanner().plan(inp).legs, ())

    def test_notifier_sink_maps_alert(self):
        from app.alerts.notifier import Alert, Severity

        seen = []

        class N:
            def notify(self, a):
                seen.append(a)
        NotifierAlertSink(N()).emit(AlertEvent("critical", "zz_kind_without_template", {"age_hours": 40}, dedup_key="k"))
        a = seen[0]
        self.assertIsInstance(a, Alert)
        self.assertEqual((a.severity, a.key, a.coin), (Severity.CRITICAL, "k", None))
        self.assertIn("age_hours=40", a.data["detail"])


if __name__ == "__main__":
    unittest.main()
