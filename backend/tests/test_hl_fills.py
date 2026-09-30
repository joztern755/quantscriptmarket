"""app.hl.fills on real (pseudonymized) mainnet fills and funding. Real cloids are swapped for platform cloids so
attribution can be exercised on genuine numbers."""
from __future__ import annotations

import copy
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.hl.client import make_cloid  # noqa: E402
from app.hl.fake import load_fixture  # noqa: E402
from app.hl.fills import (  # noqa: E402
    SubscriptionWindow, attribute_fills, attribute_funding, parse_fill, parse_funding, pnl_summary,
    position_before, position_timeline,
)

TRADER = "0x1111111111111111111111111111111111111111"
CL_START, CL_END = 1789767689556, 1789906785101
SV_START, SV_END = 1788379202323, 1788407924945


def fills_with_platform_cloids():
    """Fixture fills; each real cloid of the CL / SILVER episodes is mapped to a platform cloid of sub-CL / sub-SV."""
    raw = copy.deepcopy(load_fixture("userFills_sample"))
    cmap: dict[str, str] = {}
    for f in raw:
        if f.get("cloid") and f["coin"] in ("xyz:CL", "xyz:SILVER") and "builderFee" in f and \
                (CL_START <= f["time"] <= CL_END or SV_START <= f["time"] <= SV_END):
            sub = "sub-CL" if f["coin"] == "xyz:CL" else "sub-SV"
            new = make_cloid(sub, 0, f["cloid"])
            f["cloid"] = new
            cmap[new] = sub
    return raw, cmap


class FieldSemantics(unittest.TestCase):
    def test_fee_includes_builder_fee(self) -> None:
        """On builder fills, fee − builderFee is the plain exchange fee (same rate as this account's non-builder
        fills), and builderFee is exactly 10 bp = f 100 (0.1 %)."""
        for raw in load_fixture("userFills_sample"):
            f = parse_fill(raw)
            if "builderFee" not in raw or not f.is_perp:
                continue
            bf_bp = f.builder_fee / f.notional * 10000
            self.assertAlmostEqual(float(bf_bp), 10.0, places=2, msg=raw["coin"])
            self.assertGreater(f.fee, f.builder_fee)
            self.assertLess((f.fee - f.builder_fee) / f.notional * 10000, Decimal(5))

    def test_optional_fields(self) -> None:
        raws = load_fixture("userFills_sample")
        self.assertTrue(any("builderFee" not in r for r in raws))
        self.assertTrue(any("cloid" not in r for r in raws))
        liq = [parse_fill(r) for r in raws if "liquidation" in r]
        self.assertTrue(liq and liq[0].is_liquidation and liq[0].builder_fee == 0 and liq[0].cloid is None)
        spot = [parse_fill(r) for r in raws if r["coin"].startswith("@")]
        self.assertTrue(spot and not spot[0].is_perp)

    def test_net_pnl_micro_single_rounding(self) -> None:
        f = parse_fill({"coin": "BTC", "px": "1", "sz": "1", "side": "A", "time": 1, "closedPnl": "0.0000019",
                        "fee": "0.0000005", "tid": 1, "oid": 1, "feeToken": "USDC"})
        self.assertEqual(f.net_pnl_micro, 1)             # floor(1.4 µ) — not floor(1.9) − ceil(0.5)
        g = parse_fill({"coin": "BTC", "px": "1", "sz": "1", "side": "A", "time": 1, "closedPnl": "-1.0000001",
                        "fee": "0", "tid": 1, "oid": 1, "feeToken": "USDC"})
        self.assertEqual(g.net_pnl_micro, -1_000_001)    # floor toward −∞: losses never shrink

    def test_parse_errors(self) -> None:
        with self.assertRaises(ValidationFailed):
            parse_fill({"coin": "BTC", "side": "X"})
        with self.assertRaises(ValidationFailed):
            parse_fill({"coin": "BTC", "px": 1.5, "sz": "1", "side": "B", "time": 1, "closedPnl": "0", "fee": "0",
                        "tid": 1, "oid": 1})


class Attribution(unittest.TestCase):
    def setUp(self) -> None:
        self.raw, self.cmap = fills_with_platform_cloids()
        self.res = attribute_fills(self.raw + self.raw[:3], trading_address=TRADER.upper().replace("0X", "0x"),
                                   cloid_to_subscription=self.cmap)

    def test_real_episodes(self) -> None:
        by = self.res.by_subscription()
        cl, sv = by["sub-CL"], by["sub-SV"]
        self.assertEqual(len(cl), 8)
        self.assertEqual(len(sv), 2)
        s = pnl_summary(cl)
        self.assertEqual(s["realized_micro"], -47_605_507)   # Σ closedPnl −40.862545 − Σ fee 6.742962
        self.assertEqual(s["fees_micro"], 6_742_962)
        self.assertEqual(s["builder_fees_micro"], 6_206_707)
        self.assertEqual(pnl_summary(sv)["realized_micro"], 1_246_596)  # 1.54088 − 0.294284
        self.assertTrue(all(a.via == "cloid" and a.trading_address == TRADER for a in cl))

    def test_everything_else_is_foreign_or_rejected(self) -> None:
        self.assertEqual(len(self.res.ours_unmatched), 0)
        coins = {f.coin for f in self.res.foreign}
        self.assertIn("BTC", coins)           # third-party cloid → not ours
        self.assertEqual(len(self.res.attributed), 10)  # duplicates (same tid) dropped

    def test_window_fallback_and_unmatched(self) -> None:
        raw = copy.deepcopy(self.raw)
        # our prefix but no recorded order: REVIEW_MONEY H2 — never attributed by time window any more (a user can put
        # our prefix on their own orders); the window only becomes an alert hint
        win = SubscriptionWindow("sub-CL", TRADER, frozenset({"xyz:CL"}), CL_START - 1, CL_END + 1)
        res = attribute_fills(raw, trading_address=TRADER, cloid_to_subscription={}, windows=[win])
        self.assertEqual(res.attributed, [])
        self.assertEqual(len(res.ours_unmatched), 10)  # all prefixed fills → unmatched → alert
        self.assertEqual(sorted(set(res.window_hints.values())), ["sub-CL"])
        self.assertEqual(len(res.window_hints), 8)

    def test_non_usdc_fee_rejected(self) -> None:
        raw = copy.deepcopy(self.raw)
        for f in raw:
            if f.get("cloid") in self.cmap:
                f["feeToken"] = "HYPE"
        res = attribute_fills(raw, trading_address=TRADER, cloid_to_subscription=self.cmap)
        self.assertEqual(res.attributed, [])
        self.assertEqual(len(res.rejected), 10)


class Timeline(unittest.TestCase):
    def test_same_ms_fills_out_of_tid_order(self) -> None:
        raw, cmap = fills_with_platform_cloids()
        fills = [a.fill for a in attribute_fills(raw, trading_address=TRADER, cloid_to_subscription=cmap).attributed]
        tl = position_timeline(fills, "xyz:CL")
        self.assertEqual(tl, [(1789767689556, Decimal("-20.896")), (1789902607692, Decimal("11.211")),
                              (1789906785101, Decimal("0.000"))])
        self.assertEqual(position_before(tl, 1789767689556), 0)
        self.assertEqual(position_before(tl, 1789767689557), Decimal("-20.896"))
        self.assertEqual(position_before(tl, 1789906785102), 0)


class Funding(unittest.TestCase):
    def setUp(self) -> None:
        raw, cmap = fills_with_platform_cloids()
        att = attribute_fills(raw, trading_address=TRADER, cloid_to_subscription=cmap)
        self.fills = [a.fill for a in att.attributed]
        self.funding = load_fixture("userFunding_sample")

    def test_parse_hourly_and_aggregate(self) -> None:
        evs = [parse_funding(x) for x in self.funding]
        self.assertTrue(any(e.is_aggregate for e in evs))
        self.assertTrue(any(not e.is_aggregate for e in evs))
        for e in evs:  # sign convention: usdc = −szi × rate × px
            self.assertTrue(e.usdc == 0 or (e.usdc > 0) == (e.szi * e.funding_rate < 0))

    def test_silver_aggregates_match_holding_hours(self) -> None:
        """Opened 20:00:02 UTC, closed 03:58 next day → buckets with nSamples 3 and 4; our share is 100 %."""
        res = attribute_funding(self.funding, subscription_id="sub-SV", fills=self.fills, coins=["xyz:SILVER"],
                                start_ms=0)
        self.assertEqual([(a.time_ms, a.usdc_micro, a.share, a.estimated) for a in res.attributed],
                         [(1788307200000, -2712, Decimal(1), True), (1788393600000, -3374, Decimal(1), True)])
        self.assertEqual(res.anomalies, [])
        self.assertEqual(res.total_micro, -6086)

    def test_cl_partial_day_share_and_income_policy(self) -> None:
        res = attribute_funding(self.funding, subscription_id="sub-CL", fills=self.fills, coins=["xyz:CL"],
                                start_ms=0)
        got = {a.time_ms: a for a in res.attributed}
        # day of entry (21:41 UTC): we held −20.896 at 22:00 and 23:00; the account averaged −7.90425 over 16 h
        share = Decimal("41.792") / (Decimal("7.90425") * 16)
        self.assertEqual(got[1789689600000].share, share)
        self.assertEqual(got[1789689600000].usdc_micro, int((Decimal("-0.13246") * share * 1_000_000)
                                                            .to_integral_value(rounding="ROUND_FLOOR")))
        # full day held → share 1, but estimated INCOME is dropped by default
        self.assertEqual(got[1789776000000].share, Decimal(1))
        self.assertEqual(got[1789776000000].usdc_micro, 0)
        # the flip day mixes short and long samples → reported, cost-only (income → 0)
        self.assertEqual(got[1789862400000].usdc_micro, 0)
        self.assertEqual(len(res.anomalies), 1)
        est = attribute_funding(self.funding, subscription_id="sub-CL", fills=self.fills, coins=["xyz:CL"],
                                start_ms=0, aggregate_income="estimate")
        self.assertEqual({a.time_ms: a.usdc_micro for a in est.attributed}[1789776000000], 97348)

    def test_hourly_pro_rata_and_anomaly(self) -> None:
        hourly = [e for e in self.funding if e["delta"]["nSamples"] is None]  # ETH, account szi −0.241
        short = parse_fill({"coin": "ETH", "px": "2700", "sz": "0.1205", "side": "A", "time": 1790740000000,
                            "closedPnl": "0", "fee": "0", "tid": 9, "oid": 9, "feeToken": "USDC"})
        res = attribute_funding(hourly, subscription_id="s", fills=[short], coins=["ETH"], start_ms=0)
        self.assertEqual([a.share for a in res.attributed], [Decimal("0.5")] * 3)
        self.assertEqual([a.usdc_micro for a in res.attributed], [4021, 4029, 4009])  # floor(half of each)
        long = parse_fill({**short.raw, "side": "B"})
        bad = attribute_funding(hourly, subscription_id="s", fills=[long], coins=["ETH"], start_ms=0)
        self.assertEqual(bad.attributed, [])
        self.assertEqual(len(bad.anomalies), 3)
        window = attribute_funding(hourly, subscription_id="s", fills=[short], coins=["ETH"],
                                   start_ms=1790748000000, end_ms=1790751600012)
        self.assertEqual(len(window.attributed), 1)

    def test_bad_policy(self) -> None:
        with self.assertRaises(ValidationFailed):
            attribute_funding([], subscription_id="s", fills=[], coins=[], start_ms=0, aggregate_income="x")


if __name__ == "__main__":
    unittest.main()
