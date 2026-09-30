"""app.hl.fake: scripted exchange behaviour and consistency with app.hl.fills (real-shaped fills)."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.hl.client import BuilderCode, make_cloid  # noqa: E402
from app.hl.fake import (  # noqa: E402
    FakeExchangeGateway, FakeGatewayFactory, FakeHyperliquid, FakeInfo, FakeTransportError, load_fixture,
)
from app.hl.fills import attribute_fills, attribute_funding, parse_fill  # noqa: E402
from app.hl.markets import MarketCatalog  # noqa: E402
from app.hl.readers import HlOrderStatusReader, HlPositionReader  # noqa: E402

MASTER = "0x" + "1" * 40
SUB = "0x" + "3" * 40
B = BuilderCode("0x" + "b" * 40, 100)


def catalog() -> MarketCatalog:
    m, c = load_fixture("metaAndAssetCtxs")
    mx, cx = load_fixture("metaAndAssetCtxs_xyz")
    return MarketCatalog.build(load_fixture("perpDexs"), {"": m, "xyz": mx}, {"": c, "xyz": cx},
                               datetime(2026, 9, 30, tzinfo=timezone.utc))


def setup(vault: str | None = None):
    hl = FakeHyperliquid(catalog=catalog())
    hl.set_mid("xyz:SILVER", "61.343")
    return hl, FakeExchangeGateway(hl, MASTER, vault, builder=B)


def cl(i: int) -> str:
    return make_cloid("sub-1", 0, f"xyz:SILVER|{i}")


class Behaviour(unittest.TestCase):
    def test_full_fill_open_and_close_with_fees(self) -> None:
        hl, gw = setup()
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="10", limit_px="61.649", reduce_only=False, cloid=cl(0))
        self.assertEqual((r.status, r.filled_sz, r.avg_px), ("filled", Decimal("10"), Decimal("61.343")))
        self.assertEqual(hl.position(MASTER, "xyz:SILVER"), Decimal("10"))
        hl.set_mid("xyz:SILVER", "62")
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=False, sz="10", limit_px="61.69", reduce_only=True, cloid=cl(1))
        self.assertEqual(r.status, "filled")
        f_open, f_close = (parse_fill(f) for f in hl.account(MASTER).fills)
        self.assertEqual(f_open.dir, "Open Long")
        self.assertEqual(f_close.dir, "Close Long")
        self.assertEqual(f_close.closed_pnl, Decimal("6.57"))                    # (62 − 61.343) × 10
        self.assertEqual(f_close.builder_fee, Decimal("0.62"))                   # 0.1 % of 620
        self.assertEqual(f_close.fee, Decimal("0.279") + Decimal("0.62"))        # taker 0.045 % + builder
        self.assertEqual(f_close.net_pnl_micro, 5_671_000)
        self.assertTrue(all(o["builder"] == {"b": "0x" + "b" * 40, "f": 100} for o in hl.orders_log))

    def test_partial_no_match_reject_err_status_resting(self) -> None:
        hl, gw = setup()
        hl.script("partial", ratio="0.3")
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="10", limit_px="61.649", reduce_only=False, cloid=cl(0))
        self.assertEqual((r.status, r.filled_sz), ("partial", Decimal("3")))
        hl.script("no_match")
        self.assertEqual(gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False,
                                      cloid=cl(1)).status, "rejected")
        hl.script("reject", error="Insufficient margin to place order.")
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False, cloid=cl(2))
        self.assertEqual((r.status, r.error), ("rejected", "Insufficient margin to place order."))
        hl.script("err_status", error="Builder fee has not been approved.")
        self.assertEqual(gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False,
                                      cloid=cl(3)).status, "rejected")
        hl.script("resting")
        self.assertEqual(gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False,
                                      cloid=cl(4)).status, "resting")
        # limit below the mid for a buy never crosses
        self.assertEqual(gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.2", reduce_only=False,
                                      cloid=cl(5)).status, "rejected")
        self.assertEqual(hl.position(MASTER, "xyz:SILVER"), Decimal("3"))

    def test_reduce_only_and_duplicate_cloid(self) -> None:
        hl, gw = setup()
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=False, sz="1", limit_px="61.04", reduce_only=True, cloid=cl(0))
        self.assertEqual(r.status, "rejected")
        gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="2", limit_px="61.649", reduce_only=False, cloid=cl(1))
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=False, sz="5", limit_px="61.04", reduce_only=True, cloid=cl(2))
        self.assertEqual((r.status, r.filled_sz), ("partial", Decimal("2")))   # clamped to the position
        self.assertEqual(hl.position(MASTER, "xyz:SILVER"), 0)
        r = gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False, cloid=cl(1))
        self.assertEqual(r.status, "rejected")

    def test_transport_errors_and_resolution_by_cloid(self) -> None:
        hl, gw = setup()
        info = FakeInfo(hl)
        hl.script("raise")
        with self.assertRaises(FakeTransportError):
            gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False, cloid=cl(0))
        self.assertIsNone(HlOrderStatusReader(info).order_status_by_cloid(MASTER, cl(0)))   # never reached exchange
        hl.script("raise_after_fill")
        with self.assertRaises(FakeTransportError):
            gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz="1", limit_px="61.649", reduce_only=False, cloid=cl(1))
        res = HlOrderStatusReader(info).order_status_by_cloid(MASTER, cl(1))
        self.assertEqual((res.status, res.filled_sz), ("filled", Decimal("1")))

    def test_guards_mirror_sdk_gateway(self) -> None:
        hl, gw = setup()
        for kw in (dict(sz="1.001", limit_px="61.649", cloid=cl(0)), dict(sz="1", limit_px="61.6491", cloid=cl(0)),
                   dict(sz="1", limit_px="61.649", cloid="0x" + "0" * 32)):
            with self.assertRaises(ValidationFailed):
                gw.place_ioc(coin="xyz:SILVER", is_buy=True, reduce_only=False, **kw)
        with self.assertRaises(ValidationFailed):
            FakeExchangeGateway(hl, MASTER, builder=None)  # type: ignore[arg-type]
        with self.assertRaises(ValidationFailed):
            gw.update_leverage(coin="xyz:HOOD", leverage=2, is_cross=True)
        self.assertTrue(gw.update_leverage(coin="xyz:SILVER", leverage=2, is_cross=True).ok)

    def test_factory_sub_account_and_positions(self) -> None:
        hl = FakeHyperliquid(catalog=catalog())
        hl.set_mid("xyz:SILVER", "61.343")
        fac = FakeGatewayFactory(hl, B)
        gw = fac.create(bytearray(32), MASTER, SUB)
        gw.place_ioc(coin="xyz:SILVER", is_buy=False, sz="4", limit_px="61.04", reduce_only=False, cloid=cl(0))
        self.assertEqual(fac.created, [(MASTER, SUB)])
        self.assertEqual(hl.orders_log[-1]["vault"], SUB)
        pos = HlPositionReader(FakeInfo(hl)).positions(SUB, ["xyz:SILVER", "BTC"])
        self.assertEqual(pos["xyz:SILVER"].szi, Decimal("-4"))
        self.assertEqual(pos["xyz:SILVER"].notional_micro, -245_372_000)
        self.assertEqual(hl.position(MASTER, "xyz:SILVER"), 0)
        with self.assertRaises(ValidationFailed):
            fac.create(None, MASTER, None)


class EndToEnd(unittest.TestCase):
    def test_fake_fills_attribute_and_fund(self) -> None:
        hl, gw = setup()
        cmap = {}
        for i, (buy, sz, px) in enumerate(((True, "5", "61.649"), (True, "5", "61.649"), (False, "10", "61.04"))):
            c = cl(i)
            cmap[c] = "sub-1"
            gw.place_ioc(coin="xyz:SILVER", is_buy=buy, sz=sz, limit_px=px, reduce_only=not buy, cloid=c)
            if i == 1:
                hl.tick(3_600_000)
                hl.add_funding(MASTER, "xyz:SILVER", "0.0000125")
                hl.tick(3_600_000)
        # the user's own manual trade on the same account is not ours
        hl.apply_fill(MASTER, "xyz:SILVER", True, Decimal("1"), Decimal("61.343"), cloid=None, oid=1, builder=None)
        info = FakeInfo(hl)
        att = attribute_fills(info.user_fills_by_time(MASTER, 0), trading_address=MASTER, cloid_to_subscription=cmap)
        self.assertEqual(len(att.attributed), 3)
        self.assertEqual(len(att.foreign), 1)
        fills = [a.fill for a in att.attributed]
        fund = attribute_funding(info.user_funding(MASTER, 0), subscription_id="sub-1", fills=fills,
                                 coins=["xyz:SILVER"], start_ms=0)
        self.assertEqual(fund.total_micro, -7668)   # −10 × 61.343 × 0.0000125 = −0.00766787 → floor
        self.assertEqual(sum(a.net_pnl_micro for a in att.attributed),
                         -sum(int(Decimal(f["fee"]) * 1_000_000) for f in hl.account(MASTER).fills[:3]))


if __name__ == "__main__":
    unittest.main()
