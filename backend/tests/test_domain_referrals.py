"""Tests for app.domain.referrals (SPEC §1.2)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Economics, ReferralTier  # noqa: E402
from app.domain import referrals as r  # noqa: E402
from app.money import usd  # noqa: E402

TIERS = Economics().referral_tiers


class TierTest(unittest.TestCase):
    def name(self, users, notional, tiers=TIERS):
        return r.evaluate_tier(users, notional, tiers).name

    def test_default_starter(self):
        self.assertEqual(self.name(0, 0), "starter")
        self.assertEqual(r.evaluate_tier(0, 0).name, "starter")  # default tiers from Economics

    def test_partner_either_condition(self):
        self.assertEqual(self.name(10, 0), "partner")
        self.assertEqual(self.name(0, usd(1_000_000)), "partner")
        self.assertEqual(self.name(9, usd(1_000_000) - 1), "starter")

    def test_elite_either_condition(self):
        self.assertEqual(self.name(100, 0), "elite")
        self.assertEqual(self.name(0, usd(25_000_000)), "elite")
        self.assertEqual(self.name(99, usd(25_000_000) - 1), "partner")
        self.assertEqual(self.name(5, usd(30_000_000)), "elite")

    def test_order_independent(self):
        rev = tuple(reversed(TIERS))
        for u, n in ((0, 0), (10, 0), (100, 0), (3, usd(2_000_000))):
            self.assertEqual(self.name(u, n, rev), self.name(u, n))

    def test_misconfigured(self):
        with self.assertRaises(ValueError):
            r.evaluate_tier(0, 0, ())
        with self.assertRaises(ValueError):
            r.evaluate_tier(0, 0, (ReferralTier("x", 5, usd(5), 5000),))
        with self.assertRaises(ValueError):
            r.evaluate_tier(-1, 0)

    def test_reward_from_pool(self):
        starter, partner, elite = TIERS
        self.assertEqual(r.reward_from_pool(usd(2), starter), usd(1))
        self.assertEqual(r.reward_from_pool(usd(2), partner), usd("1.5"))
        self.assertEqual(r.reward_from_pool(usd(2), elite), usd(2))
        self.assertEqual(r.reward_from_pool(3, partner), 2)  # floored


class CodeTest(unittest.TestCase):
    def test_generate(self):
        codes = {r.generate_referral_code() for _ in range(500)}
        self.assertGreater(len(codes), 495)
        for c in codes:
            self.assertEqual(len(c), 8)
            self.assertTrue(set(c) <= set(r.CODE_ALPHABET))
            self.assertFalse(set(c) & set("0O1IL"))
        with self.assertRaises(ValueError):
            r.generate_referral_code(4)

    def test_alphabet_unambiguous(self):
        self.assertEqual(len(set(r.CODE_ALPHABET)), len(r.CODE_ALPHABET))
        self.assertFalse(set(r.CODE_ALPHABET) & set("0O1IL"))

    def test_normalize(self):
        self.assertEqual(r.normalize_referral_code(" abcd-efgh "), "ABCDEFGH")
        self.assertIsNone(r.normalize_referral_code("ABCDEFGO"))  # O not in alphabet
        self.assertIsNone(r.normalize_referral_code("ABCDEFG"))
        self.assertIsNone(r.normalize_referral_code("ABCDEFGHJ"))
        self.assertIsNone(r.normalize_referral_code(None))
        self.assertIsNone(r.normalize_referral_code("<script>"))
        code = r.generate_referral_code()
        self.assertEqual(r.normalize_referral_code(code.lower()), code)


class SelfReferralTest(unittest.TestCase):
    def test_same_user(self):
        a = r.ReferralIdentity.of("u1")
        self.assertEqual(r.self_referral_reasons(a, r.ReferralIdentity.of("u1")), ("same_user",))

    def test_same_wallet_case_insensitive(self):
        a = r.ReferralIdentity.of("u1", wallets=["0xABCdef0000000000000000000000000000000001"])
        b = r.ReferralIdentity.of("u2", wallets=["0xabcDEF0000000000000000000000000000000001"])
        self.assertTrue(r.is_self_referral(a, b))
        self.assertEqual(r.self_referral_reasons(a, b), ("same_wallet",))
        # raw constructor (not normalized) also compares case-insensitively
        c = r.ReferralIdentity("u3", wallet_addresses=frozenset({"0XABCDEF0000000000000000000000000000000001"}))
        self.assertTrue(r.is_self_referral(a, c))

    def test_same_device(self):
        a = r.ReferralIdentity.of("u1", devices=["hashA", "hashB"])
        b = r.ReferralIdentity.of("u2", devices=["hashB"])
        self.assertEqual(r.self_referral_reasons(a, b), ("same_device",))

    def test_blank_values_never_match(self):
        a = r.ReferralIdentity.of("u1", wallets=["", None, "  "], devices=["", None])
        b = r.ReferralIdentity.of("u2", wallets=["", None], devices=[""])
        self.assertFalse(r.is_self_referral(a, b))
        raw_a = r.ReferralIdentity("u1", frozenset({""}), frozenset({""}))
        raw_b = r.ReferralIdentity("u2", frozenset({""}), frozenset({""}))
        self.assertFalse(r.is_self_referral(raw_a, raw_b))
        self.assertFalse(r.is_self_referral(r.ReferralIdentity(""), r.ReferralIdentity("")))

    def test_distinct(self):
        a = r.ReferralIdentity.of("u1", ["0x1"], ["d1"])
        b = r.ReferralIdentity.of("u2", ["0x2"], ["d2"])
        self.assertFalse(r.is_self_referral(a, b))

    def test_multiple_reasons(self):
        a = r.ReferralIdentity.of("u1", ["0x1"], ["d1"])
        self.assertEqual(r.self_referral_reasons(a, a), ("same_user", "same_wallet", "same_device"))


class FirstTouchTest(unittest.TestCase):
    T = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def test_window(self):
        self.assertTrue(r.first_touch_valid(self.T, self.T))
        self.assertTrue(r.first_touch_valid(self.T, self.T + timedelta(days=30)))
        self.assertFalse(r.first_touch_valid(self.T, self.T + timedelta(days=30, seconds=1)))
        self.assertFalse(r.first_touch_valid(self.T, self.T - timedelta(seconds=1)))

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            r.first_touch_valid(datetime(2026, 9, 1), self.T)


if __name__ == "__main__":
    unittest.main()
