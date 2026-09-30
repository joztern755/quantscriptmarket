"""Tests for app.domain.fees (stdlib unittest)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Economics  # noqa: E402
from app.domain import fees  # noqa: E402
from app.errors import ValidationFailed  # noqa: E402
from app.money import usd  # noqa: E402


class BuilderFeeTest(unittest.TestCase):
    def test_default_rate_is_ten_bps(self):
        self.assertEqual(fees.builder_fee_micro(usd(10_000), 100), usd(10))

    def test_floor_rounding(self):
        # 0.999999 USD × 0.1% = 999.999 micro → 999
        self.assertEqual(fees.builder_fee_micro(999_999, 100), 999)
        self.assertEqual(fees.builder_fee_micro(1, 100), 0)

    def test_sign_of_notional_ignored(self):
        self.assertEqual(fees.builder_fee_micro(-usd(5_000), 100), usd(5))

    def test_rate_bounds(self):
        self.assertEqual(fees.builder_fee_micro(usd(1000), 0), 0)
        self.assertEqual(fees.builder_fee_micro(usd(1000), 50), usd("0.5"))
        with self.assertRaises(ValidationFailed):
            fees.builder_fee_micro(usd(1000), 101)
        with self.assertRaises(ValueError):
            fees.builder_fee_micro(usd(1000), -1)

    def test_rejects_float(self):
        with self.assertRaises(TypeError):
            fees.builder_fee_micro(1000.0, 100)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            fees.builder_fee_micro(True, 100)  # type: ignore[arg-type]


class SplitBuilderFeeTest(unittest.TestCase):
    def test_third_party_with_starter_referrer(self):
        s = fees.split_builder_fee(usd(10), in_house=False, referrer_share_of_pool_bps=5000)
        self.assertEqual(s, {"creator": usd(5), "platform": usd(4), "referrer": usd(1)})

    def test_third_party_elite_referrer_gets_whole_pool(self):
        s = fees.split_builder_fee(usd(10), in_house=False, referrer_share_of_pool_bps=10_000)
        self.assertEqual(s, {"creator": usd(5), "platform": usd(3), "referrer": usd(2)})

    def test_no_referrer_pool_to_platform(self):
        s = fees.split_builder_fee(usd(10), in_house=False, referrer_share_of_pool_bps=None)
        self.assertEqual(s, {"creator": usd(5), "platform": usd(5), "referrer": 0})

    def test_in_house_creator_part_to_platform(self):
        s = fees.split_builder_fee(usd(10), in_house=True, referrer_share_of_pool_bps=7500)
        self.assertEqual(s, {"creator": 0, "platform": usd("8.5"), "referrer": usd("1.5")})
        s = fees.split_builder_fee(usd(10), in_house=True, referrer_share_of_pool_bps=None)
        self.assertEqual(s, {"creator": 0, "platform": usd(10), "referrer": 0})

    def test_odd_amount_remainder_to_platform(self):
        # 7 micro: creator floor(3.5)=3, platform floor(2.1)=2, pool floor(1.4)=1, remainder 1 → platform
        s = fees.split_builder_fee(7, in_house=False, referrer_share_of_pool_bps=7500)
        self.assertEqual(s, {"creator": 3, "platform": 4, "referrer": 0})

    def test_exact_sum_exhaustive(self):
        for fee in list(range(0, 400)) + [999_999, 1_000_001, 123_456_789]:
            for share in (None, 0, 1, 5000, 7500, 9999, 10_000):
                for in_house in (False, True):
                    s = fees.split_builder_fee(fee, in_house, share)
                    self.assertEqual(sum(s.values()), fee, (fee, share, in_house))
                    self.assertTrue(all(v >= 0 for v in s.values()))
                    self.assertLessEqual(s["referrer"], fee * 2000 // 10_000 + 1)
                    if in_house:
                        self.assertEqual(s["creator"], 0)
                    if share is None:
                        self.assertEqual(s["referrer"], 0)

    def test_invalid(self):
        with self.assertRaises(ValidationFailed):
            fees.split_builder_fee(100, False, 10_001)
        with self.assertRaises(ValueError):
            fees.split_builder_fee(-1, False, None)
        with self.assertRaises(ValueError):
            fees.split_builder_fee(100, False, -1)

    def test_custom_economics(self):
        e = Economics(builder_split_creator_bps=6000, builder_split_platform_bps=2000, builder_split_referral_pool_bps=2000)
        s = fees.split_builder_fee(usd(10), False, 5000, e)
        self.assertEqual(s, {"creator": usd(6), "platform": usd(3), "referrer": usd(1)})


class SubscriptionSplitTest(unittest.TestCase):
    def test_three_percent(self):
        self.assertEqual(fees.subscription_split(usd(20)), (usd("19.4"), usd("0.6")))

    def test_remainder_to_platform(self):
        self.assertEqual(fees.subscription_split(1), (0, 1))
        self.assertEqual(fees.subscription_split(33), (32, 1))
        for p in range(0, 1000):
            c, pl = fees.subscription_split(p)
            self.assertEqual(c + pl, p)
            self.assertGreaterEqual(pl, p * 300 // 10_000)

    def test_free(self):
        self.assertEqual(fees.subscription_split(0), (0, 0))

    def test_negative(self):
        with self.assertRaises(ValueError):
            fees.subscription_split(-1)


class PostSplitTest(unittest.TestCase):
    def test_min_price_sale(self):
        self.assertEqual(fees.post_sale_split(usd(2)), (usd(1), usd(1)))
        self.assertEqual(fees.post_sale_split(usd("9.99")), (usd("8.99"), usd(1)))

    def test_below_min_rejected(self):
        for p in (0, 1, usd(2) - 1, usd(1)):
            with self.assertRaises(ValidationFailed):
                fees.post_sale_split(p)

    def test_validate_post_price(self):
        self.assertEqual(fees.validate_post_price(0), 0)  # free post
        self.assertEqual(fees.validate_post_price(usd(2)), usd(2))
        with self.assertRaises(ValidationFailed):
            fees.validate_post_price(usd("1.5"))


class PlanTest(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(fees.plan_price("free"), 0)
        self.assertEqual(fees.plan_price("pro"), usd(20))
        self.assertEqual(fees.plan_price("max"), usd(50))
        with self.assertRaises(ValidationFailed):
            fees.plan_price("gold")

    def test_strategy_limits(self):
        self.assertTrue(fees.plan_allows_active_strategies("free", 1))
        self.assertFalse(fees.plan_allows_active_strategies("free", 2))
        self.assertTrue(fees.plan_allows_active_strategies("pro", 3))
        self.assertFalse(fees.plan_allows_active_strategies("pro", 4))
        self.assertTrue(fees.plan_allows_active_strategies("max", 10_000))
        with self.assertRaises(ValidationFailed):
            fees.plan_allows_active_strategies("gold", 1)


if __name__ == "__main__":
    unittest.main()
