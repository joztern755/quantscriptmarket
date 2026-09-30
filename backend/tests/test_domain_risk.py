"""Tests for app.domain.risk (SPEC §5.4 guards, §10 sizing)."""
from __future__ import annotations

import random
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import RiskLimits  # noqa: E402
from app.domain.risk import (  # noqa: E402
    MarketSnapshot, OrderPlan, Rejection, RiskFlags, SubscriptionContext, liquidity_cap_micro, plan_order,
    round_px_toward_mid,
)
from app.money import usd  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 0, 5, tzinfo=UTC)
COIN = "xyz:SILVER"
L = RiskLimits()


def mkt(**kw) -> MarketSnapshot:
    base = dict(coin=COIN, mid_px=D("30"), mark_px=D("30"), oracle_px=D("30"), day_ntl_vlm_micro=usd(50_000_000),
                open_interest_micro=usd(20_000_000), max_leverage=10, sz_decimals=2, is_delisted=False,
                data_time=NOW - timedelta(seconds=5))
    base.update(kw)
    return MarketSnapshot(**base)


def ctx(**kw) -> SubscriptionContext:
    base = dict(allocation_micro=usd(10_000), max_leverage_x100=200, status="active", current_position_notional_micro=0,
                consecutive_rejects=0, allowed_markets=frozenset({COIN}))
    base.update(kw)
    return SubscriptionContext(**base)


def plan(w, c=None, m=None, f=None, limits=L, now=NOW):
    return plan_order(w, c or ctx(), m or mkt(), f or RiskFlags(), limits, now)


class SizingTest(unittest.TestCase):
    def one(self, res) -> OrderPlan:
        self.assertIsInstance(res, list, res)
        self.assertEqual(len(res), 1, res)
        return res[0]

    def test_open_long_from_flat(self):
        p = self.one(plan(10_000))
        self.assertEqual((p.coin, p.side, p.notional_micro, p.reduce_only, p.close_position), (COIN, "buy", usd(10_000), False, False))
        self.assertEqual(p.limit_px, D("30.15"))
        self.assertEqual(p.sz, D("333.33"))
        self.assertEqual(p.notes, ())

    def test_open_short(self):
        p = self.one(plan(-10_000))
        self.assertEqual((p.side, p.notional_micro, p.reduce_only), ("sell", usd(10_000), False))
        self.assertEqual(p.limit_px, D("29.85"))

    def test_weight_two_within_user_leverage(self):
        self.assertEqual(self.one(plan(20_000)).notional_micro, usd(20_000))

    def test_leverage_clamps(self):
        p = self.one(plan(20_000, ctx(max_leverage_x100=100)))
        self.assertEqual(p.notional_micro, usd(10_000))
        self.assertIn("leverage_clamped", p.notes)
        self.assertEqual(self.one(plan(20_000, m=mkt(max_leverage=1))).notional_micro, usd(10_000))
        self.assertEqual(self.one(plan(20_000, ctx(strategy_max_leverage_x100=150))).notional_micro, usd(15_000))
        self.assertEqual(self.one(plan(-20_000, ctx(max_leverage_x100=50))).notional_micro, usd(5_000))
        # platform cap 5x
        self.assertEqual(self.one(plan(50_000, ctx(max_leverage_x100=1000))).notional_micro, usd(50_000))
        self.assertEqual(self.one(plan(50_000, ctx(max_leverage_x100=1000), limits=replace(L, platform_max_leverage=3)),
                                  ).notional_micro, usd(30_000))

    def test_weight_above_platform_cap_is_clamped(self):
        p = self.one(plan(60_000, ctx(max_leverage_x100=1000)))
        self.assertEqual(p.notional_micro, usd(50_000))
        self.assertIn("leverage_clamped", p.notes)

    def test_absurd_weight_rejected(self):
        r = plan(1_000_001, ctx(max_leverage_x100=1000))
        self.assertIsInstance(r, Rejection)
        self.assertIn("weight_out_of_range", r.reasons)
        self.assertIsInstance(plan(-1_000_000), list)

    def test_increase_and_reduce(self):
        p = self.one(plan(10_000, ctx(current_position_notional_micro=usd(5_000))))
        self.assertEqual((p.side, p.notional_micro, p.reduce_only), ("buy", usd(5_000), False))
        p = self.one(plan(5_000, ctx(current_position_notional_micro=usd(10_000))))
        self.assertEqual((p.side, p.notional_micro, p.reduce_only, p.close_position), ("sell", usd(5_000), True, False))
        p = self.one(plan(-5_000, ctx(current_position_notional_micro=-usd(10_000))))
        self.assertEqual((p.side, p.notional_micro, p.reduce_only), ("buy", usd(5_000), True))
        p = self.one(plan(-10_000, ctx(current_position_notional_micro=-usd(5_000))))
        self.assertEqual((p.side, p.reduce_only), ("sell", False))

    def test_full_close(self):
        p = self.one(plan(0, ctx(current_position_notional_micro=usd(10_000))))
        self.assertEqual((p.side, p.notional_micro, p.reduce_only, p.close_position), ("sell", usd(10_000), True, True))
        p = self.one(plan(0, ctx(current_position_notional_micro=-usd(10_000))))
        self.assertEqual((p.side, p.reduce_only, p.close_position), ("buy", True, True))
        self.assertEqual(p.sz, D("333.34"))  # close sizes round up (reduce-only caps at position)

    def test_dust_close_bypasses_thresholds(self):
        p = self.one(plan(0, ctx(current_position_notional_micro=usd(5))))
        self.assertEqual((p.notional_micro, p.close_position, p.reduce_only), (usd(5), True, True))
        p = self.one(plan(0, ctx(current_position_notional_micro=1)))   # 1 micro: sz floors to 0 → smallest lot
        self.assertEqual(p.sz, D("0.01"))

    def test_rebalance_threshold(self):
        # threshold = max($10, 2% of $10k = $200)
        self.assertEqual(plan(10_000, ctx(current_position_notional_micro=usd(9_800) + 1)), [])
        self.one(plan(10_000, ctx(current_position_notional_micro=usd(9_800))))   # delta exactly $200
        self.assertEqual(plan(9_000, ctx(current_position_notional_micro=usd(9_100))), [])  # reduce also below

    def test_small_allocation_min_order(self):
        c = ctx(allocation_micro=usd(100))
        self.assertEqual(plan(900, c), [])            # $9 < $10
        self.assertEqual(self.one(plan(1_000, c)).notional_micro, usd(10))

    def test_zero_allocation(self):
        self.assertEqual(plan(10_000, ctx(allocation_micro=0)), [])
        p = self.one(plan(10_000, ctx(allocation_micro=0, current_position_notional_micro=usd(50))))
        self.assertTrue(p.close_position)

    def test_nothing_to_do(self):
        self.assertEqual(plan(0), [])
        self.assertEqual(plan(10_000, ctx(current_position_notional_micro=usd(10_000))), [])

    def test_btc_like_integer_price(self):
        m = mkt(coin="BTC", mid_px=D("100000"), mark_px=D("100000"), oracle_px=D("100000"), sz_decimals=5)
        p = self.one(plan(10_000, ctx(allowed_markets={"BTC"}), m))
        self.assertEqual((p.limit_px, p.sz), (D("100500"), D("0.1")))
        self.assertEqual(str(p.limit_px), "100500")


class FlipTest(unittest.TestCase):
    def test_long_to_short(self):
        r = plan(-5_000, ctx(current_position_notional_micro=usd(10_000)))
        self.assertEqual(len(r), 2)
        close, open_ = r
        self.assertEqual((close.side, close.notional_micro, close.reduce_only, close.close_position), ("sell", usd(10_000), True, True))
        self.assertEqual((open_.side, open_.notional_micro, open_.reduce_only, open_.close_position), ("sell", usd(5_000), False, False))

    def test_short_to_long(self):
        close, open_ = plan(20_000, ctx(current_position_notional_micro=-usd(3_000)))
        self.assertEqual((close.side, close.close_position, open_.side, open_.notional_micro), ("buy", True, "buy", usd(20_000)))

    def test_small_open_leg_dropped(self):
        r = plan(-100, ctx(current_position_notional_micro=usd(10_000)))   # target -$100 < $200 threshold
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].close_position)
        self.assertIn("open_leg_below_threshold", r[0].notes)

    def test_tiny_flip_still_closes(self):
        r = plan(-100, ctx(current_position_notional_micro=usd(5)))
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].close_position)

    def test_flip_with_clamped_close_does_not_open(self):
        m = mkt(day_ntl_vlm_micro=usd(1_000_000))   # cap $5,000
        r = plan(-10_000, ctx(current_position_notional_micro=usd(10_000)), m)
        self.assertEqual(len(r), 1)
        self.assertEqual((r[0].side, r[0].notional_micro, r[0].reduce_only, r[0].close_position), ("sell", usd(5_000), True, False))
        self.assertIn("liquidity_clamped", r[0].notes)

    def test_flip_open_leg_clamped(self):
        m = mkt(day_ntl_vlm_micro=usd(3_000_000))   # cap $15,000
        close, open_ = plan(-20_000, ctx(current_position_notional_micro=usd(1_000)), m)
        self.assertTrue(close.close_position)
        self.assertEqual(open_.notional_micro, usd(15_000))
        self.assertIn("liquidity_clamped", open_.notes)


class EntryBlockTest(unittest.TestCase):
    def check_reduce_only_semantics(self, c_kw=None, f=None):
        c_kw = c_kw or {}
        self.assertEqual(plan(10_000, ctx(**c_kw), f=f), [])                                   # no entry
        self.assertEqual(plan(20_000, ctx(current_position_notional_micro=usd(10_000), **c_kw), f=f), [])  # no increase
        p = plan(5_000, ctx(current_position_notional_micro=usd(10_000), **c_kw), f=f)
        self.assertEqual((len(p), p[0].side, p[0].reduce_only), (1, "sell", True))
        r = plan(-10_000, ctx(current_position_notional_micro=usd(10_000), **c_kw), f=f)      # flip → close only
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].close_position)
        self.assertIn("entries_blocked", r[0].notes)
        p = plan(0, ctx(current_position_notional_micro=-usd(10_000), **c_kw), f=f)
        self.assertEqual((p[0].side, p[0].close_position), ("buy", True))

    def test_reduce_only_status(self):
        self.check_reduce_only_semantics({"status": "reduce_only"})

    def test_past_due_after_grace(self):
        self.check_reduce_only_semantics({"status": "past_due", "past_due_since": NOW - timedelta(hours=72)})

    def test_past_due_missing_since(self):
        self.check_reduce_only_semantics({"status": "past_due"})

    def test_past_due_within_grace_trades_normally(self):
        p = plan(10_000, ctx(status="past_due", past_due_since=NOW - timedelta(hours=1)))
        self.assertEqual((len(p), p[0].reduce_only), (1, False))

    def test_new_entries_paused(self):
        self.check_reduce_only_semantics(f=RiskFlags(new_entries_paused=True))

    def test_market_paused(self):
        self.check_reduce_only_semantics(f=RiskFlags(paused_markets=frozenset({COIN})))
        self.assertEqual(len(plan(10_000, f=RiskFlags(paused_markets=frozenset({"BTC"})))), 1)


class HardBlockTest(unittest.TestCase):
    def rej(self, res, reason):
        self.assertIsInstance(res, Rejection, res)
        self.assertIn(reason, res.reasons)

    def test_global_kill_blocks_even_closes(self):
        self.rej(plan(10_000, f=RiskFlags(global_kill=True)), "kill_switch_global")
        self.rej(plan(0, ctx(current_position_notional_micro=usd(10_000)), f=RiskFlags(global_kill=True)), "kill_switch_global")

    def test_market_kill(self):
        self.rej(plan(0, ctx(current_position_notional_micro=usd(10_000)), f=RiskFlags(killed_markets=frozenset({COIN}))),
                 "kill_switch_market")
        self.assertIsInstance(plan(10_000, f=RiskFlags(killed_markets=frozenset({"BTC"}))), list)

    def test_breaker(self):
        self.rej(plan(10_000, ctx(consecutive_rejects=3)), "circuit_breaker_open")
        self.rej(plan(0, ctx(consecutive_rejects=7, current_position_notional_micro=usd(100))), "circuit_breaker_open")
        self.assertIsInstance(plan(10_000, ctx(consecutive_rejects=2)), list)

    def test_statuses(self):
        self.rej(plan(10_000, ctx(status="paused_user")), "subscription_paused_user")
        self.rej(plan(0, ctx(status="cancelled", current_position_notional_micro=usd(100))), "subscription_cancelled")
        self.rej(plan(10_000, ctx(status="pending")), "subscription_pending")
        self.rej(plan(10_000, ctx(status="weird")), "subscription_status_unknown")

    def test_whitelist(self):
        self.rej(plan(10_000, ctx(allowed_markets=frozenset({"BTC"}))), "market_not_whitelisted")
        self.rej(plan(10_000, ctx(allowed_markets=frozenset())), "market_not_whitelisted")
        self.rej(plan(10_000, ctx(allowed_markets=frozenset({"xyz:silver"}))), "market_not_whitelisted")  # case-sensitive

    def test_delisted(self):
        self.rej(plan(0, ctx(current_position_notional_micro=usd(100)), mkt(is_delisted=True)), "market_delisted")

    def test_stale_data(self):
        self.assertIsInstance(plan(10_000, m=mkt(data_time=NOW - timedelta(seconds=60))), list)
        self.rej(plan(10_000, m=mkt(data_time=NOW - timedelta(seconds=61))), "stale_data")
        self.assertIsInstance(plan(10_000, m=mkt(data_time=NOW + timedelta(seconds=3))), list)
        self.rej(plan(10_000, m=mkt(data_time=NOW + timedelta(seconds=10))), "data_time_in_future")

    def test_mark_oracle_deviation(self):
        self.assertIsInstance(plan(10_000, m=mkt(mark_px=D("30.6"))), list)          # exactly 2%
        self.rej(plan(10_000, m=mkt(mark_px=D("30.61"))), "mark_oracle_deviation")
        self.rej(plan(10_000, m=mkt(mark_px=D("29.39"))), "mark_oracle_deviation")
        r = plan(10_000, m=mkt(mid_px=D("31")))
        self.rej(r, "mid_oracle_deviation")
        self.assertNotIn("mark_oracle_deviation", r.reasons)

    def test_all_reasons_collected(self):
        r = plan(10_000, ctx(consecutive_rejects=5), mkt(data_time=NOW - timedelta(hours=1), is_delisted=True),
                 RiskFlags(global_kill=True))
        self.assertEqual(set(r.reasons) >= {"kill_switch_global", "circuit_breaker_open", "stale_data", "market_delisted"}, True)

    def test_invalid_inputs_fail_closed(self):
        self.rej(plan(10_000, m=mkt(mid_px=30.0)), "invalid_input:mid_px")
        self.rej(plan(10_000, m=mkt(oracle_px=D("0"))), "invalid_input:oracle_px")
        self.rej(plan(10_000, m=mkt(mark_px=D("NaN"))), "invalid_input:mark_px")
        self.rej(plan(10_000, m=mkt(data_time=datetime(2026, 9, 30))), "invalid_input:data_time")
        self.rej(plan(10_000, ctx(allocation_micro=-1)), "invalid_input:allocation_micro")
        self.rej(plan(10_000, ctx(allocation_micro=10.5)), "invalid_input:allocation_micro")
        self.rej(plan(True), "invalid_input:target_weight_bps")
        self.rej(plan(1.0), "invalid_input:target_weight_bps")
        self.rej(plan(10_000, ctx(max_leverage_x100=0)), "invalid_input:max_leverage_x100")
        self.rej(plan(10_000, m=mkt(max_leverage=0)), "invalid_input:max_leverage")
        self.rej(plan(10_000, m=mkt(sz_decimals=7)), "invalid_input:sz_decimals")
        self.rej(plan(10_000, m=mkt(day_ntl_vlm_micro=-5)), "invalid_input:day_ntl_vlm_micro")
        self.rej(plan(10_000, now=datetime(2026, 9, 30)), "invalid_input:now")


class LiquidityTest(unittest.TestCase):
    def test_cap_values(self):
        self.assertEqual(liquidity_cap_micro(mkt(day_ntl_vlm_micro=usd(1_000_000), open_interest_micro=usd(10_000_000)), L), usd(5_000))
        self.assertEqual(liquidity_cap_micro(mkt(day_ntl_vlm_micro=usd(100_000_000), open_interest_micro=usd(100_000)), L), usd(2_000))

    def test_volume_clamp(self):
        (p,) = plan(10_000, m=mkt(day_ntl_vlm_micro=usd(1_000_000)))
        self.assertEqual(p.notional_micro, usd(5_000))
        self.assertIn("liquidity_clamped", p.notes)

    def test_oi_clamp(self):
        (p,) = plan(10_000, m=mkt(open_interest_micro=usd(100_000)))
        self.assertEqual(p.notional_micro, usd(2_000))

    def test_close_clamped_is_partial(self):
        (p,) = plan(0, ctx(current_position_notional_micro=usd(10_000)), mkt(day_ntl_vlm_micro=usd(1_000_000)))
        self.assertEqual((p.notional_micro, p.reduce_only, p.close_position), (usd(5_000), True, False))

    def test_too_thin_rejects(self):
        r = plan(10_000, m=mkt(open_interest_micro=0))
        self.assertEqual(r, Rejection(("liquidity_cap",)))
        r = plan(10_000, m=mkt(day_ntl_vlm_micro=usd(1_999)))    # cap $9.995 < $10
        self.assertEqual(r, Rejection(("liquidity_cap",)))
        r = plan(0, ctx(current_position_notional_micro=usd(100)), mkt(open_interest_micro=0))
        self.assertEqual(r, Rejection(("liquidity_cap",)))


class PriceTest(unittest.TestCase):
    def test_round_px(self):
        self.assertEqual(round_px_toward_mid(D("30.15"), 2, True), D("30.15"))
        self.assertEqual(round_px_toward_mid(D("3.14159265"), 2, True), D("3.1415"))
        self.assertEqual(round_px_toward_mid(D("3.14151"), 2, False), D("3.1416"))
        self.assertEqual(round_px_toward_mid(D("100500.7"), 5, True), D("100500"))
        self.assertEqual(round_px_toward_mid(D("123456.1"), 0, False), D("123457"))
        self.assertEqual(round_px_toward_mid(D("0.0123456"), 0, False), D("0.012346"))
        self.assertEqual(round_px_toward_mid(D("0.0123456"), 3, True), D("0.012"))   # max 3 decimals
        self.assertEqual(round_px_toward_mid(D("9.99996"), 2, False), D("10"))

    def test_limit_within_slippage_and_on_grid(self):
        rng = random.Random(7)
        for _ in range(500):
            szd = rng.randint(0, 5)
            mid = D(rng.randint(1, 10**9)) / D(10) ** rng.randint(0, 6)
            m = mkt(mid_px=mid, mark_px=mid, oracle_px=mid, sz_decimals=szd)
            for w in (10_000, -10_000):
                res = plan(w, m=m)
                if isinstance(res, Rejection):
                    self.assertEqual(res.reasons, ("price_unrepresentable",))
                    continue
                for p in res:
                    if p.side == "buy":
                        self.assertLessEqual(p.limit_px, mid * D("1.005"))
                    else:
                        self.assertGreaterEqual(p.limit_px, mid * D("0.995"))
                    self.assertGreater(p.limit_px, 0)
                    exp = p.limit_px.as_tuple().exponent
                    self.assertLessEqual(max(0, -exp), 6 - szd)
                    if p.limit_px != p.limit_px.to_integral_value():
                        self.assertLessEqual(len(p.limit_px.normalize().as_tuple().digits), 5)
                    self.assertNotIn("E", str(p.limit_px))
                    self.assertGreater(p.notional_micro, 0)


if __name__ == "__main__":
    unittest.main()
