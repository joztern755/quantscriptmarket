"""app.hl.markets against recorded mainnet fixtures (2026-09-30)."""
from __future__ import annotations

import os
import random
import sys
import unittest
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.domain.risk import MarketSnapshot, round_px_toward_mid  # noqa: E402
from app.errors import ExternalServiceError, GuardRejected, NotFound, ValidationFailed  # noqa: E402
from app.hl.fake import load_fixture  # noqa: E402
from app.hl.markets import (  # noqa: E402
    MarketCatalog, asset_id_for, canonical, format_px, format_sz, ioc_limit_px, is_valid_px, is_valid_sz,
)

T0 = datetime(2026, 9, 30, 7, 10, tzinfo=timezone.utc)


def catalog() -> MarketCatalog:
    m, c = load_fixture("metaAndAssetCtxs")
    mx, cx = load_fixture("metaAndAssetCtxs_xyz")
    return MarketCatalog.build(load_fixture("perpDexs"), {"": m, "xyz": mx}, {"": c, "xyz": cx}, T0)


class AssetIds(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = catalog()

    def test_perp_dexs_position_zero_is_validator_dex(self) -> None:
        pd = load_fixture("perpDexs")
        self.assertIsNone(pd[0])
        self.assertEqual(pd[1]["name"], "xyz")
        self.assertEqual(MarketCatalog.dex_index_map(pd)["xyz"], 1)

    def test_launch_markets(self) -> None:
        expect = {"BTC": 0, "SOL": 5, "HYPE": 159, "xyz:GOLD": 110003, "xyz:SILVER": 110026, "xyz:CL": 110029,
                  "xyz:BRENTOIL": 110049}
        for coin, aid in expect.items():
            self.assertEqual(self.cat.asset_id(coin), aid, coin)
            self.assertEqual(self.cat.coin_for_asset(aid), coin)
        self.assertEqual(self.cat.market("xyz:SILVER").dex, "xyz")
        self.assertEqual(self.cat.market("xyz:SILVER").sz_decimals, 2)
        self.assertEqual(self.cat.market("xyz:SILVER").max_leverage, 25)
        self.assertEqual(self.cat.market("BTC").sz_decimals, 5)
        self.assertEqual(self.cat.market("BTC").max_leverage, 40)

    def test_formula(self) -> None:
        self.assertEqual(asset_id_for(0, 7), 7)
        self.assertEqual(asset_id_for(1, 0), 110000)
        self.assertEqual(asset_id_for(3, 12), 130012)
        with self.assertRaises(ValidationFailed):
            asset_id_for(1, 10000)

    def test_unknown_coin(self) -> None:
        with self.assertRaises(NotFound):
            self.cat.market("xyz:NOPE")
        self.assertNotIn("SILVER", self.cat)  # builder-dex coins only exist with their prefix

    def test_rejects_perp_dexs_without_null_head(self) -> None:
        with self.assertRaises(ExternalServiceError):
            MarketCatalog.dex_index_map([{"name": "xyz"}])

    def test_rejects_builder_coin_without_prefix(self) -> None:
        bad = {"universe": [{"name": "SILVER", "szDecimals": 2, "maxLeverage": 25}]}
        with self.assertRaises(ExternalServiceError):
            MarketCatalog.build(load_fixture("perpDexs"), {"xyz": bad})

    def test_ctx_universe_length_mismatch(self) -> None:
        m, c = load_fixture("metaAndAssetCtxs")
        with self.assertRaises(ExternalServiceError):
            MarketCatalog.build(load_fixture("perpDexs"), {"": m}, {"": c[:-1]})

    def test_duplicate_name_prefers_live_listing(self) -> None:
        meta = {"universe": [{"name": "AAA", "szDecimals": 1, "maxLeverage": 3, "isDelisted": True},
                             {"name": "AAA", "szDecimals": 2, "maxLeverage": 5}]}
        cat = MarketCatalog.build([None], {"": meta})
        self.assertEqual(cat.asset_id("AAA"), 1)
        both_live = {"universe": [{"name": "AAA", "szDecimals": 1, "maxLeverage": 3},
                                  {"name": "AAA", "szDecimals": 2, "maxLeverage": 5}]}
        with self.assertRaises(ExternalServiceError):
            MarketCatalog.build([None], {"": both_live})


class Flags(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = catalog()

    def test_launch_markets_allow_cross(self) -> None:
        for coin in ("BTC", "SOL", "HYPE", "xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:BRENTOIL"):
            m = self.cat.market(coin)
            self.assertFalse(m.only_isolated, coin)
            self.assertTrue(m.allows_cross, coin)
            self.assertFalse(m.is_delisted, coin)

    def test_hip3_isolated_only(self) -> None:
        m = self.cat.market("xyz:HOOD")
        self.assertTrue(m.only_isolated)
        self.assertEqual(m.margin_mode, "noCross")
        self.assertFalse(m.allows_cross)

    def test_delisted(self) -> None:
        self.assertTrue(self.cat.market("MATIC").is_delisted)
        u = self.cat.market("xyz:URANIUM")
        self.assertTrue(u.is_delisted)
        self.assertEqual(u.margin_mode, "strictIsolated")


class Formatting(unittest.TestCase):
    def test_size_rounds_down(self) -> None:
        self.assertEqual(format_sz("0.123456789", 5), "0.12345")
        self.assertEqual(format_sz(Decimal("2.069"), 2), "2.06")
        self.assertEqual(format_sz("10", 2), "10")
        self.assertEqual(format_sz("0.009", 2), "0")
        self.assertEqual(format_sz("1.50", 2), "1.5")
        with self.assertRaises(ValidationFailed):
            format_sz("-1", 2)
        with self.assertRaises(ValidationFailed):
            format_sz(1.5, 2)  # type: ignore[arg-type]  # floats refused

    def test_price_rules(self) -> None:
        # BTC szDecimals 5 → ≤1 decimal, ≤5 sig figs; integers always allowed
        self.assertEqual(format_px("82971.54", 5, is_buy=True), "82971")
        self.assertEqual(format_px("82971.54", 5, is_buy=False), "82972")
        self.assertEqual(format_px("123456.7", 5, is_buy=True), "123456")
        # SILVER szDecimals 2 → ≤4 decimals, 5 sig figs → 61.343
        self.assertEqual(format_px("61.34349", 2, is_buy=True), "61.343")
        self.assertEqual(format_px("61.34349", 2, is_buy=False), "61.344")
        # small prices: decimals cap (6 − szDecimals) binds before sig figs
        self.assertEqual(format_px("0.000123456", 0, is_buy=True), "0.000123")
        self.assertEqual(format_px("0.0123456", 2, is_buy=True), "0.0123")
        # carry adds a digit: 9.99995 ceil → 10.000 → canonical "10"
        self.assertEqual(format_px("9.99995", 1, is_buy=False), "10")
        self.assertEqual(format_px("100.0", 2, rounding=ROUND_DOWN), "100")
        with self.assertRaises(ValidationFailed):
            format_px("1.0", 2)  # direction required
        with self.assertRaises(ValidationFailed):
            format_px("0.0000001", 5, is_buy=True)  # rounds to zero

    def test_matches_risk_module_rounding(self) -> None:
        rnd = random.Random(7)
        for _ in range(3000):
            szd = rnd.randint(0, 5)
            px = Decimal(rnd.randint(1, 10 ** 9)).scaleb(-rnd.randint(0, 9))
            for is_buy in (True, False):
                theirs = round_px_toward_mid(px, szd, is_buy)
                if theirs == 0:  # we fail closed instead of returning a zero price
                    with self.assertRaises(ValidationFailed):
                        format_px(px, szd, is_buy=is_buy)
                    continue
                self.assertEqual(format_px(px, szd, is_buy=is_buy), canonical(theirs), (px, szd, is_buy))

    def test_validity(self) -> None:
        self.assertTrue(is_valid_px("82971", 5))
        self.assertTrue(is_valid_px("123456", 5))      # integer beyond 5 sig figs is fine
        self.assertFalse(is_valid_px("82971.5", 5))    # 6 sig figs
        self.assertTrue(is_valid_px("61.343", 2))
        self.assertFalse(is_valid_px("61.3431", 2))
        self.assertFalse(is_valid_px("0", 2))
        self.assertTrue(is_valid_sz("2.06", 2))
        self.assertFalse(is_valid_sz("2.061", 2))
        self.assertFalse(is_valid_sz("0", 2))
        rnd = random.Random(11)
        for _ in range(500):
            szd = rnd.randint(0, 5)
            px = Decimal(rnd.randint(1, 10 ** 8)).scaleb(-rnd.randint(0, 8))
            self.assertTrue(is_valid_px(format_px(px, szd, is_buy=True), szd))

    def test_ioc_limit_within_slippage_cap(self) -> None:
        mid = Decimal("61.343")
        buy = Decimal(ioc_limit_px(mid, 2, is_buy=True, slippage_bps=50))
        sell = Decimal(ioc_limit_px(mid, 2, is_buy=False, slippage_bps=50))
        self.assertLessEqual(buy, mid * Decimal("1.005"))
        self.assertGreater(buy, mid)
        self.assertGreaterEqual(sell, mid * Decimal("0.995"))
        self.assertLess(sell, mid)
        self.assertEqual(str(buy), "61.649")
        self.assertEqual(str(sell), "61.037")


class Snapshot(unittest.TestCase):
    def test_btc_from_fixture(self) -> None:
        snap = catalog().to_snapshot("BTC")
        self.assertIsInstance(snap, MarketSnapshot)
        self.assertEqual(snap.mid_px, Decimal("82971.5"))
        self.assertEqual(snap.mark_px, Decimal("82966.3"))
        self.assertEqual(snap.oracle_px, Decimal("83006.0"))
        self.assertEqual(snap.day_ntl_vlm_micro, 2119207834_281432)  # floored
        oi = Decimal("35330.49274") * Decimal("82966.3")
        self.assertEqual(snap.open_interest_micro, int((oi * 1_000_000).to_integral_value(rounding="ROUND_FLOOR")))
        self.assertEqual((snap.max_leverage, snap.sz_decimals, snap.is_delisted), (40, 5, False))
        self.assertEqual(snap.data_time, T0)

    def test_silver_oi_in_usd(self) -> None:
        cat = catalog()
        snap = cat.to_snapshot("xyz:SILVER")
        self.assertEqual(snap.mid_px, Decimal("61.343"))
        usd = Decimal("2547926.2400000002") * Decimal("61.344")
        self.assertEqual(snap.open_interest_micro, int(usd * 1_000_000))
        self.assertEqual(cat.ctx("xyz:SILVER").open_interest_usd, usd)

    def test_no_mid_fails_closed(self) -> None:
        cat = catalog()
        self.assertIsNone(cat.ctx("MATIC").mid_px)
        with self.assertRaises(GuardRejected):
            cat.to_snapshot("MATIC")
        with self.assertRaises(GuardRejected):
            cat.ioc_limit_px("MATIC", is_buy=True, slippage_bps=50)

    def test_from_info_fetches_requested_dexes_only(self) -> None:
        from app.hl.fake import FakeInfo

        info = FakeInfo()
        cat = MarketCatalog.from_info(info, MarketCatalog.dexes_for(["xyz:SILVER", "BTC"]), now=T0)
        self.assertEqual([c[0] for c in info.calls], ["perp_dexs", "meta_and_asset_ctxs", "meta_and_asset_ctxs"])
        self.assertEqual(cat.asset_id("xyz:SILVER"), 110026)
        self.assertEqual(cat.dex_names[:2], ("", "xyz"))


if __name__ == "__main__":
    unittest.main()
