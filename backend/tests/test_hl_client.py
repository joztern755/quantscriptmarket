"""app.hl.client: cloids, builder code on every order, SDK gateway (stubbed SDK), response normalisation."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.errors import ValidationFailed  # noqa: E402
from app.hl.client import (  # noqa: E402
    CLOID_PREFIX, BuilderCode, SdkExchangeGateway, SdkGatewayFactory, is_platform_cloid, make_cloid,
    normalize_order_response, wire_float,
)
from app.hl.fake import load_fixture  # noqa: E402
from app.hl.markets import MarketCatalog  # noqa: E402

BUILDER = BuilderCode("0x" + "b" * 40, 100)
MASTER = "0x" + "1" * 40
SUB = "0x" + "3" * 40
BAR = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


def catalog() -> MarketCatalog:
    m, c = load_fixture("metaAndAssetCtxs")
    mx, cx = load_fixture("metaAndAssetCtxs_xyz")
    return MarketCatalog.build(load_fixture("perpDexs"), {"": m, "xyz": mx}, {"": c, "xyz": cx},
                               datetime(2026, 9, 30, tzinfo=timezone.utc))


class StubExchange:
    def __init__(self, response=None):
        self.calls: list[tuple] = []
        self.expires: list[int] = []
        self.response = response or {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": "2.06", "avgPx": "61.344", "oid": 42}}]}}}

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None, builder=None):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type, reduce_only, cloid, builder))
        return self.response

    def update_leverage(self, leverage, name, is_cross=True):
        self.calls.append(("update_leverage", leverage, name, is_cross))
        return {"status": "ok", "response": {"type": "default"}}

    def set_expires_after(self, ms):
        self.expires.append(ms)


class Clock:
    def __init__(self, ms: int = 1_790_000_000_000):
        self.ms = ms
        self.sleeps = 0

    def now(self) -> int:
        return self.ms

    def sleep(self, _s: float) -> None:
        self.sleeps += 1
        self.ms += 1


def gateway(ex=None, clock=None) -> tuple[SdkExchangeGateway, StubExchange, Clock]:
    ex = ex or StubExchange()
    clock = clock or Clock()
    gw = SdkExchangeGateway(ex, catalog(), BUILDER, cloid_factory=lambda c: ("Cloid", c), now_ms=clock.now,
                            sleep=clock.sleep)
    return gw, ex, clock


class Cloids(unittest.TestCase):
    def test_deterministic_prefixed_16_bytes(self) -> None:
        a = make_cloid("sub-1", BAR, "xyz:SILVER|0")
        self.assertEqual(a, make_cloid("sub-1", int(BAR.timestamp() * 1000), "xyz:SILVER|0"))
        self.assertRegex(a, r"^0x[0-9a-f]{32}$")
        self.assertTrue(a.startswith("0x" + CLOID_PREFIX))
        self.assertTrue(is_platform_cloid(a))
        self.assertTrue(is_platform_cloid(a.upper().replace("0X", "0x")))
        others = {make_cloid("sub-2", BAR, "xyz:SILVER|0"), make_cloid("sub-1", BAR, "xyz:SILVER|1"),
                  make_cloid("sub-1", 1, "xyz:SILVER|0"), make_cloid("sub-1", BAR, "BTC|0")}
        self.assertNotIn(a, others)
        self.assertEqual(len(others), 4)

    def test_prefix_matches_executor(self) -> None:
        from app.execution import executor

        self.assertEqual(executor.CLOID_PREFIX, CLOID_PREFIX)
        self.assertTrue(is_platform_cloid(executor.make_cloid("s", BAR, "BTC", 0)))

    def test_foreign_cloids(self) -> None:
        for c in (None, "", "0x703783d9db984d6ab6f9b5075b6718ba", "0xa17a1000", "a17a1000" + "0" * 24):
            self.assertFalse(is_platform_cloid(c))

    def test_naive_bar_close_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            make_cloid("s", datetime(2026, 1, 1), "x")


class Builder(unittest.TestCase):
    def test_builder_validation(self) -> None:
        for addr, fee in (("", 100), ("0x" + "B" * 40, 100), ("0x" + "b" * 40, 0), ("0x" + "b" * 40, 101),
                          ("0x" + "b" * 40, True)):
            with self.assertRaises(ValidationFailed):
                BuilderCode(addr, fee)  # type: ignore[arg-type]
        self.assertEqual(BUILDER.wire(), {"b": "0x" + "b" * 40, "f": 100})

    def test_gateway_requires_builder(self) -> None:
        with self.assertRaises(ValidationFailed):
            SdkExchangeGateway(StubExchange(), catalog(), None)  # type: ignore[arg-type]


class WireFloat(unittest.TestCase):
    def test_exact_values_pass(self) -> None:
        for s in ("61.343", "82971", "0.00012", "2.06", "0.12345"):
            self.assertEqual(Decimal(repr(wire_float(s))), Decimal(s))

    def test_unrepresentable_refused(self) -> None:
        with self.assertRaises(ValidationFailed):
            wire_float("0.000000001")  # 9 decimals: SDK would round


class Normalize(unittest.TestCase):
    def test_variants(self) -> None:
        r = normalize_order_response({"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 77747314}}]}}}, Decimal("0.02"))
        self.assertEqual((r.status, r.filled_sz, r.avg_px, r.oid), ("filled", Decimal("0.02"), Decimal("1891.4"),
                                                                    77747314))
        r = normalize_order_response({"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": "0.01", "avgPx": "1891.4", "oid": 1}}]}}}, Decimal("0.02"))
        self.assertEqual(r.status, "partial")
        r = normalize_order_response({"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"error": "Order could not immediately match against any resting orders. asset=0"}]}}}, Decimal(1))
        self.assertEqual(r.status, "rejected")
        self.assertIn("could not immediately match", r.error)
        r = normalize_order_response({"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"resting": {"oid": 5}}]}}}, Decimal(1))
        self.assertEqual((r.status, r.oid), ("resting", 5))
        r = normalize_order_response({"status": "err", "response": "Builder fee has not been approved."}, Decimal(1))
        self.assertEqual(r.status, "rejected")
        for weird in (None, "x", {"status": "ok"}, {"status": "ok", "response": {"data": {"statuses": ["waiting"]}}},
                      {"status": "ok", "response": {"data": {"statuses": [{"waitingForTrigger": {}}]}}}):
            self.assertEqual(normalize_order_response(weird, Decimal(1)).status, "unknown")


class Gateway(unittest.TestCase):
    def test_every_order_carries_builder_and_is_ioc(self) -> None:
        gw, ex, _ = gateway()
        cloid = make_cloid("sub", BAR, "xyz:SILVER|0")
        res = gw.place_ioc(coin="xyz:SILVER", is_buy=True, sz=Decimal("2.06"), limit_px="61.649", reduce_only=False,
                           cloid=cloid)
        self.assertEqual(res.status, "filled")
        self.assertEqual(res.filled_sz, Decimal("2.06"))
        (_, name, is_buy, sz, px, ot, ro, cl, builder), = ex.calls
        self.assertEqual((name, is_buy, sz, px, ot, ro), ("xyz:SILVER", True, 2.06, 61.649, {"limit": {"tif": "Ioc"}},
                                                          False))
        self.assertEqual(cl, ("Cloid", cloid))
        self.assertEqual(builder, {"b": "0x" + "b" * 40, "f": 100})
        self.assertEqual(len(ex.expires), 1)

    def test_place_ioc_has_no_way_to_drop_builder(self) -> None:
        import inspect

        params = inspect.signature(SdkExchangeGateway.place_ioc).parameters
        self.assertNotIn("builder", params)

    def test_refuses_off_grid_delisted_and_foreign_cloid(self) -> None:
        gw, ex, _ = gateway()
        ok = make_cloid("sub", BAR, "x")
        cases = [dict(coin="xyz:SILVER", sz="2.061", limit_px="61.649", cloid=ok),       # lot
                 dict(coin="xyz:SILVER", sz="2.06", limit_px="61.6491", cloid=ok),      # tick
                 dict(coin="BTC", sz="0.001", limit_px="82971.5", cloid=ok),            # 6 sig figs
                 dict(coin="MATIC", sz="1", limit_px="0.37", cloid=ok),                 # delisted
                 dict(coin="xyz:SILVER", sz="2.06", limit_px="61.649", cloid="0x703783d9db984d6ab6f9b5075b6718ba"),
                 dict(coin="xyz:SILVER", sz="0", limit_px="61.649", cloid=ok)]
        for kw in cases:
            with self.assertRaises(ValidationFailed, msg=kw):
                gw.place_ioc(is_buy=True, reduce_only=False, **kw)
        self.assertEqual(ex.calls, [])

    def test_distinct_millisecond_nonces(self) -> None:
        gw, ex, clock = gateway()
        for i in range(3):
            gw.place_ioc(coin="BTC", is_buy=False, sz="0.001", limit_px="82000", reduce_only=True,
                         cloid=make_cloid("s", BAR, f"BTC|{i}"))
        self.assertEqual(len(set(ex.expires)), 3)
        self.assertGreaterEqual(clock.sleeps, 2)

    def test_update_leverage(self) -> None:
        gw, ex, _ = gateway()
        self.assertTrue(gw.update_leverage(coin="xyz:SILVER", leverage=2, is_cross=True).ok)
        self.assertEqual(ex.calls[-1], ("update_leverage", 2, "xyz:SILVER", True))
        with self.assertRaises(ValidationFailed):
            gw.update_leverage(coin="xyz:HOOD", leverage=2, is_cross=True)  # isolated-only HIP-3 market
        self.assertTrue(gw.update_leverage(coin="xyz:HOOD", leverage=2, is_cross=False).ok)
        with self.assertRaises(ValidationFailed):
            gw.update_leverage(coin="xyz:SILVER", leverage=26, is_cross=True)


class FakeInfoMaps:
    def __init__(self, meta):
        self.coin_to_asset = {u["name"]: i for i, u in enumerate(meta["universe"])}
        self.name_to_coin = {u["name"]: u["name"] for u in meta["universe"]}
        self.asset_to_sz_decimals = {i: u["szDecimals"] for i, u in enumerate(meta["universe"])}


class StubSdkExchange(StubExchange):
    instances: list = []

    def __init__(self, wallet, base_url=None, meta=None, vault_address=None, account_address=None, spot_meta=None,
                 perp_dexs=None, timeout=None):
        super().__init__()
        self.wallet, self.base_url, self.vault_address, self.account_address = wallet, base_url, vault_address, \
            account_address
        self.spot_meta, self.timeout = spot_meta, timeout
        self.info = FakeInfoMaps(meta)
        StubSdkExchange.instances.append(self)


class Factory(unittest.TestCase):
    def factory(self, cls=StubSdkExchange) -> SdkGatewayFactory:
        cat = catalog()
        return SdkGatewayFactory(lambda: cat, BUILDER, base_url="https://api.hyperliquid.xyz", exchange_cls=cls,
                                 account_from_key=lambda k: ("wallet", len(k)))

    def test_sub_account_uses_vault_address_and_registers_builder_dex(self) -> None:
        gw = self.factory().create(bytearray(32), MASTER, SUB)
        ex = StubSdkExchange.instances[-1]
        self.assertEqual((ex.account_address, ex.vault_address), (MASTER, SUB))
        self.assertEqual(ex.wallet, ("wallet", 32))
        self.assertEqual(ex.spot_meta, {"universe": [], "tokens": []})
        self.assertEqual(ex.info.coin_to_asset["xyz:SILVER"], 110026)
        self.assertEqual(ex.info.name_to_coin["xyz:SILVER"], "xyz:SILVER")
        self.assertEqual(ex.info.asset_to_sz_decimals[110026], 2)
        self.assertEqual(ex.info.coin_to_asset["BTC"], 0)
        self.assertIs(gw.builder, BUILDER)

    def test_master_trades_without_vault(self) -> None:
        self.factory().create(b"\x01" * 32, MASTER, MASTER)
        self.assertIsNone(StubSdkExchange.instances[-1].vault_address)

    def test_incompatible_sdk_refused(self) -> None:
        class NoVault:
            def __init__(self, wallet, base_url=None, meta=None, account_address=None):
                pass

        with self.assertRaises(ValidationFailed):
            self.factory(NoVault).create(b"\x01" * 32, MASTER, SUB)

        class NoMaps(StubSdkExchange):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.info = object()

        with self.assertRaises(ValidationFailed):
            self.factory(NoMaps).create(b"\x01" * 32, MASTER, None)

    def test_asset_id_disagreement_refused(self) -> None:
        class Disagree(StubSdkExchange):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.info.coin_to_asset["xyz:SILVER"] = 120026

        with self.assertRaises(ValidationFailed):
            self.factory(Disagree).create(b"\x01" * 32, MASTER, None)


if __name__ == "__main__":
    unittest.main()
