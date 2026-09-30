"""Tests for app.domain.profit_share (SPEC §1.1 HWM)."""
from __future__ import annotations

import random
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Economics  # noqa: E402
from app.domain.profit_share import SubscriptionPnlState, settle, user_rate_bps, validate_creator_bps  # noqa: E402
from app.errors import ValidationFailed  # noqa: E402
from app.money import usd  # noqa: E402

E = Economics()
CAP = E.profit_share_creator_cap_bps
PLAT = E.platform_profit_share_bps
CARVED = replace(E, platform_profit_share_mode="carved_out")


class OnTopTest(unittest.TestCase):
    def test_default_mode_is_on_top(self):
        self.assertEqual(E.platform_profit_share_mode, "on_top")

    def test_first_profit(self):
        r = settle(SubscriptionPnlState(), usd(1), 1000, E)
        self.assertEqual(r.profit, usd(1))
        self.assertEqual(r.charge_total, usd(1) * (1000 + PLAT) // 10_000)
        self.assertEqual(r.creator_part, usd("0.1"))
        self.assertEqual(r.platform_part, r.charge_total - usd("0.1"))
        self.assertEqual(r.new_state, SubscriptionPnlState(usd(1), usd(1)))

    def test_max_rate_at_cap(self):
        r = settle(SubscriptionPnlState(), usd(100), CAP, E)
        self.assertEqual(r.charge_total, usd(100) * (CAP + PLAT) // 10_000)
        self.assertEqual(r.creator_part, usd(100) * CAP // 10_000)

    def test_zero_creator_rate_platform_still_charges(self):
        r = settle(SubscriptionPnlState(), usd(100), 0, E)
        self.assertEqual((r.charge_total, r.creator_part, r.platform_part), (usd("1.5"), 0, usd("1.5")))

    def test_loss_then_recovery(self):
        s = SubscriptionPnlState(usd(1), usd(1))
        r = settle(s, -usd("0.5"), 1000, E)
        self.assertEqual((r.charge_total, r.profit), (0, 0))
        self.assertEqual(r.new_state, SubscriptionPnlState(usd("0.5"), usd(1)))
        r = settle(r.new_state, usd("0.3"), 1000, E)  # cum 0.8 < hwm 1.0
        self.assertEqual(r.charge_total, 0)
        self.assertEqual(r.new_state.hwm_micro, usd(1))
        r = settle(r.new_state, usd("0.7"), 1000, E)  # cum 1.5 → profit 0.5
        self.assertEqual(r.profit, usd("0.5"))
        self.assertEqual(r.charge_total, usd("0.5") * 1150 // 10_000)
        self.assertEqual(r.new_state, SubscriptionPnlState(usd("1.5"), usd("1.5")))

    def test_negative_start_never_charges_until_positive(self):
        r = settle(SubscriptionPnlState(), -1000, 1000, E)
        self.assertEqual(r.charge_total, 0)
        self.assertEqual(r.new_state, SubscriptionPnlState(-1000, 0))
        r = settle(r.new_state, 2000, 1000, E)
        self.assertEqual(r.profit, 1000)
        self.assertEqual(r.new_state, SubscriptionPnlState(1000, 1000))

    def test_tiny_profit_rounds_to_zero_but_hwm_advances(self):
        r = settle(SubscriptionPnlState(), 1, 1000, E)
        self.assertEqual(r.charge_total, 0)
        self.assertEqual(r.new_state.hwm_micro, 1)

    def test_zero_delta(self):
        s = SubscriptionPnlState(500, 500)
        r = settle(s, 0, 1000, E)
        self.assertEqual((r.charge_total, r.new_state), (0, s))

    def test_random_walk_invariants(self):
        rng = random.Random(42)
        for creator in (0, 1, 149, 150, 700, CAP):
            for econ in (E, CARVED):
                s = SubscriptionPnlState()
                total_profit = 0
                running_max = 0
                for _ in range(300):
                    d = rng.randint(-usd(50), usd(60))
                    r = settle(s, d, creator, econ)
                    self.assertGreaterEqual(r.charge_total, 0)          # losses never refund
                    self.assertEqual(r.charge_total, r.creator_part + r.platform_part)
                    self.assertGreaterEqual(r.creator_part, 0)
                    self.assertGreaterEqual(r.platform_part, 0)
                    total_profit += r.profit
                    s = r.new_state
                    running_max = max(running_max, s.cum_pnl_micro)
                    self.assertEqual(s.hwm_micro, running_max)
                self.assertEqual(total_profit, running_max)  # profit is charged once per dollar above HWM

    def test_deterministic_and_pure(self):
        s = SubscriptionPnlState(10, 5)
        a = settle(s, 12345, 800, E)
        b = settle(s, 12345, 800, E)
        self.assertEqual(a, b)
        self.assertEqual(s, SubscriptionPnlState(10, 5))

    def test_in_house(self):
        r = settle(SubscriptionPnlState(), usd(100), 1000, E, in_house=True)
        self.assertEqual(r.creator_part, 0)
        self.assertEqual(r.platform_part, r.charge_total)
        self.assertEqual(r.charge_total, usd("11.5"))


class CarvedOutTest(unittest.TestCase):
    def test_platform_out_of_creator(self):
        r = settle(SubscriptionPnlState(), usd(100), 1000, CARVED)
        self.assertEqual((r.charge_total, r.creator_part, r.platform_part), (usd(10), usd("8.5"), usd("1.5")))

    def test_creator_below_platform_rate(self):
        r = settle(SubscriptionPnlState(), usd(100), 100, CARVED)
        self.assertEqual((r.charge_total, r.creator_part, r.platform_part), (usd(1), 0, usd(1)))

    def test_creator_equal_platform_rate(self):
        r = settle(SubscriptionPnlState(), usd(100), 150, CARVED)
        self.assertEqual((r.charge_total, r.creator_part, r.platform_part), (usd("1.5"), 0, usd("1.5")))

    def test_zero(self):
        r = settle(SubscriptionPnlState(), usd(100), 0, CARVED)
        self.assertEqual((r.charge_total, r.creator_part, r.platform_part), (0, 0, 0))
        self.assertEqual(r.new_state.hwm_micro, usd(100))

    def test_rounding_remainder_to_platform(self):
        r = settle(SubscriptionPnlState(), 999, 1001, CARVED)
        self.assertEqual(r.charge_total, 999 * 1001 // 10_000)       # 99
        self.assertEqual(r.creator_part, 999 * 851 // 10_000)         # 85
        self.assertEqual(r.platform_part, r.charge_total - r.creator_part)


class ValidationTest(unittest.TestCase):
    def test_cap_from_economics(self):
        self.assertEqual(validate_creator_bps(CAP, E), CAP)
        with self.assertRaises(ValidationFailed):
            settle(SubscriptionPnlState(), 1, CAP + 1, E)
        with self.assertRaises(ValidationFailed):
            settle(SubscriptionPnlState(), 1, -1, E)
        wider = replace(E, profit_share_creator_cap_bps=CAP + 300)
        self.assertEqual(settle(SubscriptionPnlState(), usd(1), CAP + 300, wider).creator_part,
                         usd(1) * (CAP + 300) // 10_000)

    def test_unknown_mode(self):
        with self.assertRaises(ValidationFailed):
            settle(SubscriptionPnlState(), 1, 100, replace(E, platform_profit_share_mode="weird"))

    def test_types(self):
        with self.assertRaises(TypeError):
            settle(SubscriptionPnlState(), 1.5, 100, E)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            settle(SubscriptionPnlState(0.0, 0), 1, 100, E)  # type: ignore[arg-type]

    def test_user_rate(self):
        self.assertEqual(user_rate_bps(CAP, E), CAP + PLAT)
        self.assertEqual(user_rate_bps(CAP, CARVED), CAP)
        self.assertEqual(user_rate_bps(0, E), PLAT)


if __name__ == "__main__":
    unittest.main()
