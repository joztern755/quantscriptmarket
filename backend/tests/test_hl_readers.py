"""app.hl.readers: executor port adapters + on-chain approval checks (fixtures / FakeInfo, no network)."""
from __future__ import annotations

import copy
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.execution.ports import MarketSnapshot as PortSnapshot  # noqa: E402
from app.hl.fake import FakeHyperliquid, FakeInfo, load_fixture, seed_approvals  # noqa: E402
from app.hl.readers import (  # noqa: E402
    HlMarketData, HlOrderStatusReader, HlPositionReader, order_status_to_result, verify_agent_approval,
    verify_builder_approval, verify_trading_address,
)

MASTER = "0x2222222222222222222222222222222222222222"
SUB = "0x3333333333333333333333333333333333333333"
AGENT = "0x4444444444444444444444444444444444444444"
BUILDER = "0x" + "b" * 40
NOW_MS = 1_790_752_250_000


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t


class MarketDataTests(unittest.TestCase):
    def test_snapshot_port_shape_and_cache(self) -> None:
        info, clock = FakeInfo(), Clock()
        md = HlMarketData(info, clock=clock, ctx_ttl_s=15)
        snap = md.snapshot("xyz:SILVER")
        self.assertIsInstance(snap, PortSnapshot)
        self.assertEqual((snap.coin, snap.mid_px, snap.mark_px, snap.oracle_px, snap.max_leverage, snap.sz_decimals),
                         ("xyz:SILVER", Decimal("61.343"), Decimal("61.344"), Decimal("61.342"), 25, 2))
        self.assertEqual(snap.as_of, clock.t)
        self.assertGreater(snap.open_interest_notional_micro, 0)
        n = len(info.calls)
        md.snapshot("xyz:SILVER")
        md.snapshot("BTC")
        self.assertEqual(len(info.calls), n)  # cached within TTL
        clock.t += timedelta(seconds=16)
        md.snapshot("BTC")
        self.assertEqual([c[0] for c in info.calls[n:]], ["meta_and_asset_ctxs"])  # only the validator dex refreshed

    def test_unavailable_is_none(self) -> None:
        md = HlMarketData(FakeInfo())
        self.assertIsNone(md.snapshot("MATIC"))      # delisted, no mid
        self.assertIsNone(md.snapshot("xyz:NOPE"))


class PositionTests(unittest.TestCase):
    def test_from_recorded_xyz_state(self) -> None:
        class Info:
            def __init__(self):
                self.dexes = []

            def clearinghouse_state(self, address, dex=""):
                self.dexes.append(dex)
                return load_fixture("clearinghouseState_xyz_positions" if dex == "xyz" else "clearinghouseState")

        info = Info()
        pos = HlPositionReader(info).positions(MASTER, ["xyz:XYZ100", "xyz:META", "BTC", "xyz:SILVER"])
        self.assertEqual(sorted(info.dexes), ["", "xyz"])
        self.assertEqual(pos["xyz:XYZ100"].szi, Decimal("0.4043"))
        self.assertEqual(pos["xyz:XYZ100"].notional_micro, 12_288_294_200)
        self.assertEqual(pos["xyz:META"].szi, Decimal("-10.405"))
        self.assertLess(pos["xyz:META"].notional_micro, 0)
        self.assertEqual(pos["BTC"].szi, Decimal("-0.03111"))
        self.assertEqual(pos["BTC"].notional_micro, -2_582_546_874)
        self.assertEqual(pos["xyz:SILVER"].szi, 0)  # missing ⇒ flat


class OrderStatusTests(unittest.TestCase):
    def test_recorded_payloads(self) -> None:
        self.assertIsNone(order_status_to_result(load_fixture("orderStatus_unknown")))
        filled = order_status_to_result(load_fixture("orderStatus_filled"))
        self.assertEqual((filled.status, filled.filled_sz, filled.oid), ("filled", Decimal("0.03111"), 560957570068))
        fills = load_fixture("userFills_sample")
        with_fills = order_status_to_result(load_fixture("orderStatus_filled"), fills)
        # only 2 of the order's partial fills are in the sample → partial by fills, with a VWAP
        self.assertEqual(with_fills.filled_sz, Decimal("0.00108"))
        self.assertEqual(with_fills.status, "partial")
        self.assertEqual(with_fills.avg_px, Decimal("82997.0"))

    def test_status_vocabulary(self) -> None:
        for h in load_fixture("historicalOrders_sample"):
            res = order_status_to_result({"status": "order", "order": h})
            st = h["status"]
            if st == "open":
                self.assertEqual(res.status, "resting")
            elif st == "filled":
                self.assertIn(res.status, ("filled", "partial"))
            elif "Rejected" in st or "Canceled" in st:
                self.assertIn(res.status, ("rejected", "partial"), st)
            else:
                self.assertEqual(res.status, "unknown", st)

    def test_reader_uses_fills_for_avg_px(self) -> None:
        from app.hl.client import BuilderCode, make_cloid
        from app.hl.fake import FakeExchangeGateway

        hl = FakeHyperliquid()
        hl.set_mid("BTC", "83000")
        gw = FakeExchangeGateway(hl, MASTER, builder=BuilderCode(BUILDER, 100))
        cloid = make_cloid("s", 0, "BTC|0")
        hl.script("partial", ratio="0.5")
        gw.place_ioc(coin="BTC", is_buy=True, sz="0.002", limit_px="83400", reduce_only=False, cloid=cloid)
        res = HlOrderStatusReader(FakeInfo(hl)).order_status_by_cloid(MASTER, cloid)
        self.assertEqual((res.status, res.filled_sz, res.avg_px), ("partial", Decimal("0.001"), Decimal("83000")))
        self.assertIsNone(HlOrderStatusReader(FakeInfo(hl)).order_status_by_cloid(MASTER, make_cloid("s", 0, "x")))


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hl = FakeHyperliquid(now_ms=NOW_MS)
        seed_approvals(self.hl, master=MASTER, agent=AGENT, builder=BUILDER, sub_accounts=[SUB])
        self.info = FakeInfo(self.hl)

    def test_agent_ok(self) -> None:
        a = verify_agent_approval(self.info, MASTER, AGENT, now_ms=NOW_MS)
        self.assertTrue(a.approved, a.reason)
        self.assertEqual(a.name, "aijalon")
        self.assertTrue(a.expires_within(NOW_MS, 151))
        self.assertFalse(a.expires_within(NOW_MS, 149))

    def test_agent_failures(self) -> None:
        self.assertFalse(verify_agent_approval(self.info, MASTER, "0x" + "9" * 40, now_ms=NOW_MS).approved)
        self.assertFalse(verify_agent_approval(self.info, SUB, AGENT, now_ms=NOW_MS).approved)  # subs have no agents
        late = NOW_MS + 146 * 86_400_000
        self.assertEqual(verify_agent_approval(self.info, MASTER, AGENT, now_ms=late).reason,
                         "agent approval expires too soon")
        self.hl.agents[MASTER][0]["name"] = "other"
        self.assertFalse(verify_agent_approval(self.info, MASTER, AGENT, now_ms=NOW_MS).approved)
        self.hl.agents[MASTER][0]["name"] = "aijalon"
        self.hl.roles[AGENT] = {"role": "agent", "data": {"user": SUB}}
        self.assertFalse(verify_agent_approval(self.info, MASTER, AGENT, now_ms=NOW_MS).approved)

    def test_recorded_agent_shape(self) -> None:
        class Info:
            def extra_agents(self, user):
                return load_fixture("extraAgents")

            def user_role(self, user):
                return load_fixture("userRole_samples")["agent"]

        a = verify_agent_approval(Info(), "0x1111111111111111111111111111111111111111",
                                  "0x4444444444444444444444444444444444444444", now_ms=NOW_MS, expected_name="bcdc")
        self.assertTrue(a.approved, a.reason)
        self.assertEqual(a.valid_until_ms, 1800121958345)

    def test_builder(self) -> None:
        self.assertEqual(verify_builder_approval(self.info, MASTER, BUILDER).max_fee_tenths_bp, 100)
        self.assertTrue(verify_builder_approval(self.info, MASTER, BUILDER).approved)
        self.hl.builder_fees[(MASTER, BUILDER)] = 50
        self.assertFalse(verify_builder_approval(self.info, MASTER, BUILDER).approved)
        self.assertFalse(verify_builder_approval(self.info, SUB, BUILDER).approved)

    def test_trading_address(self) -> None:
        self.assertEqual(verify_trading_address(self.info, MASTER, MASTER), "master")
        self.assertEqual(verify_trading_address(self.info, MASTER, SUB.upper().replace("0X", "0x")), "subAccount")
        with self.assertRaises(ValidationFailed):
            verify_trading_address(self.info, MASTER, "0x" + "8" * 40)       # someone else's account
        other = copy.deepcopy(load_fixture("userRole_samples")["subAccount"])  # recorded shape
        other["data"]["master"] = "0x" + "9" * 40
        self.hl.roles["0x" + "8" * 40] = other                                # sub-account of a DIFFERENT master
        with self.assertRaises(ValidationFailed):
            verify_trading_address(self.info, MASTER, "0x" + "8" * 40)


if __name__ == "__main__":
    unittest.main()
