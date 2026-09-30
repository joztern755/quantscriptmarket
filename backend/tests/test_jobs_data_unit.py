"""Pure unit tests for app/jobs_data (no database, no network): thresholds, trade-event classification, candle
validation, the request-weight pacer, the stored-first backtest fetcher and the market universe."""
from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.hl.fake import FakeInfo, load_fixture  # noqa: E402
from app.hl.fills import AttributedFill, parse_fill  # noqa: E402
from app.jobs_data.agents import THRESHOLD_DAYS, threshold_for  # noqa: E402
from app.jobs_data.candles import _parse_candle, _same, live_perp_markets  # noqa: E402
from app.jobs_data.fills import trade_events  # noqa: E402
from app.jobs_data.hl import WeightPacer, candle_weight, list_weight  # noqa: E402

DAY = 86_400_000
PREFIX = "0xa17a1000"


def _fill(side: str, sz: str, start: str, *, px: str = "100.0", closed: str = "0.0", fee: str = "0.1",
          builder: str = "0.05", cloid: str = PREFIX + "0" * 24, t: int = 1_000, tid: int = 1) -> dict:
    return {"coin": "BTC", "px": px, "sz": sz, "side": side, "time": t, "startPosition": start, "dir": "x",
            "closedPnl": closed, "hash": "0x" + "ab" * 32, "oid": 7, "crossed": True, "fee": fee, "builderFee": builder,
            "tid": tid, "cloid": cloid, "feeToken": "USDC", "twapId": None}


def _att(*raws: dict, sub: str = "s1") -> list[AttributedFill]:
    return [AttributedFill(sub, "0x" + "1" * 40, parse_fill(r), "cloid") for r in raws]


class Thresholds(unittest.TestCase):
    def test_most_urgent_crossed(self) -> None:
        now = 10 * DAY * 100
        self.assertEqual(THRESHOLD_DAYS, (14, 7, 3, 1))
        self.assertIsNone(threshold_for(now + 15 * DAY, now))
        self.assertEqual(threshold_for(now + 14 * DAY, now), 14)
        self.assertEqual(threshold_for(now + 8 * DAY, now), 14)
        self.assertEqual(threshold_for(now + 7 * DAY, now), 7)
        self.assertEqual(threshold_for(now + 2 * DAY + 1, now), 3)
        self.assertEqual(threshold_for(now + 1, now), 1)
        self.assertIsNone(threshold_for(now, now))                     # expired → handled as expiry


class TradeEvents(unittest.TestCase):
    def test_opened_with_partials(self) -> None:
        ev = trade_events(_att(_fill("B", "0.001", "0.0", px="80000.0", tid=1),
                               _fill("B", "0.002", "0.001", px="80010.0", tid=2)))
        self.assertEqual(len(ev), 1)
        sub, kind, p = ev[0]
        self.assertEqual((sub, kind, p["side"], p["size"], p["avg_px"], p["position_after"], p["fills"]),
                         ("s1", "trade_opened", "buy", "0.003", "80006.66666666", "0.003", 2))
        self.assertEqual((p["fees_micro"], p["builder_fee_micro"], p["notional_micro"]), (200_000, 100_000, 240_020_000))

    def test_closed_resized_flipped(self) -> None:
        c = trade_events(_att(_fill("A", "0.003", "0.003", closed="2.97", fee="0.035")))[0]
        self.assertEqual((c[1], c[2]["realized_pnl_micro"], c[2]["net_pnl_micro"], c[2]["position_after"]),
                         ("trade_closed", 2_970_000, 2_935_000, "0"))
        r = trade_events(_att(_fill("A", "1", "3", closed="-1.5")))[0]
        self.assertEqual((r[1], r[2]["position_before"], r[2]["position_after"], r[2]["flipped"]),
                         ("trade_resized", "3", "2", False))
        f = trade_events(_att(_fill("A", "5", "2")))[0]
        self.assertEqual((f[1], f[2]["position_after"], f[2]["flipped"]), ("trade_resized", "-3", True))
        short_open = trade_events(_att(_fill("A", "2", "0.0")))[0]
        self.assertEqual((short_open[1], short_open[2]["side"], short_open[2]["position_after"]),
                         ("trade_opened", "sell", "-2"))

    def test_one_event_per_order_and_subscription(self) -> None:
        a = _fill("B", "1", "0", cloid=PREFIX + "1" * 24, tid=1, t=1)
        b = _fill("A", "1", "1", cloid=PREFIX + "2" * 24, tid=2, t=2)
        ev = trade_events(_att(a) + _att(b, sub="s2"))
        self.assertEqual([(e[0], e[1]) for e in ev], [("s1", "trade_opened"), ("s2", "trade_closed")])


class Candles(unittest.TestCase):
    def test_parse_and_validate(self) -> None:
        raw = load_fixture("candleSnapshot_xyz_SILVER_1d")[2]
        c = _parse_candle(raw, "xyz:SILVER", "1d")
        self.assertEqual((c["t"], c["o"], c["v"], c["n"]), (raw["t"], raw["o"], raw["v"], raw["n"]))
        self.assertEqual(c["v"], "1896261.1799999999")                 # exact string kept
        self.assertIsNone(_parse_candle({**raw, "o": 66.5}, "xyz:SILVER", "1d"))       # floats refused
        self.assertIsNone(_parse_candle({**raw, "t": raw["t"] + 1}, "xyz:SILVER", "1d"))  # misaligned
        self.assertIsNone(_parse_candle({**raw, "h": "1", "l": "2"}, "xyz:SILVER", "1d"))
        self.assertIsNone(_parse_candle(raw, "BTC", "1d"))              # wrong coin
        self.assertIsNone(_parse_candle(raw, "xyz:SILVER", "4h"))       # wrong interval
        self.assertIsNone(_parse_candle({**raw, "c": "0"}, "xyz:SILVER", "1d"))
        self.assertTrue(_same({"o": "66.50", "h": "1", "l": "1", "c": "1", "v": "2"},
                              {"o": "66.5", "h": "1.0", "l": "1", "c": "1", "v": "2.000"}))
        self.assertFalse(_same({"o": "66.5", "h": "1", "l": "1", "c": "1", "v": "2"},
                               {"o": "66.5", "h": "1", "l": "1", "c": "1", "v": "2.0001"}))

    def test_universe_from_recorded_meta(self) -> None:
        info = FakeInfo()
        coins, skipped = live_perp_markets(info)
        meta, meta_xyz = load_fixture("meta"), load_fixture("meta_xyz")
        live = [u["name"] for u in meta["universe"] + meta_xyz["universe"] if not u.get("isDelisted")]
        self.assertIn("BTC", coins)
        self.assertIn("xyz:SILVER", coins)
        self.assertEqual(set(coins) | {s for s in skipped if not s.startswith("dex:")}, set(live))
        self.assertTrue(any(s.startswith("dex:") for s in skipped))    # fixture has no meta for other dexes


class Pacer(unittest.TestCase):
    def test_budget_and_deadline(self) -> None:
        t = [0.0]
        slept: list[float] = []

        def sleep(s: float) -> None:
            slept.append(s)
            t[0] += s

        p = WeightPacer(600, max_seconds=30, clock=lambda: t[0], sleep=sleep)
        for _ in range(7):                                             # burst capacity = 150 → 7 × 20 = 140
            p.spend(20)
        self.assertEqual(slept, [])
        p.spend(20)                                                    # needs 10 more tokens at 10/s
        self.assertAlmostEqual(sum(slept), 1.0)
        self.assertTrue(p.can_start(20))
        t[0] = 29.9
        p._refill()
        p.tokens = 0
        self.assertFalse(p.can_start(20))                              # would overrun the deadline
        self.assertEqual((candle_weight(5000), candle_weight(3), list_weight(2000), list_weight(0)), (104, 21, 120, 20))


class StoredFirst(unittest.TestCase):
    def test_store_then_api_and_notes(self) -> None:
        from app.sandbox.backtest import StoredFirstFetcher, fetch_market_data
        from app.sandbox.validate import StrategyMeta

        rows = load_fixture("candleSnapshot_BTC_1d")
        stored, tail = rows[:-3], rows[-3:]

        class Store:
            backfilled = False

            def stored_candles(self, coin: str, iv: str, s: int, e: int) -> list[dict]:
                return [r for r in stored if s <= r["t"] <= e]

            def is_backfilled(self, coin: str, iv: str) -> bool:
                return self.backfilled

        class Api:
            def __init__(self) -> None:
                self.calls: list[tuple[int, int]] = []

            def candles(self, coin: str, iv: str, s: int, e: int) -> list[dict]:
                self.calls.append((s, e))
                return [dict(r, c="0.5") for r in rows if s <= r["t"] <= e]   # API disagrees on overlap: store wins

            def funding(self, coin: str, s: int, e: int) -> list[dict]:
                return []

        api, store = Api(), Store()
        f = StoredFirstFetcher(store, api)
        out = f.candles("BTC", "1d", 0, rows[-1]["t"])
        self.assertEqual([r["t"] for r in out], [r["t"] for r in rows])
        self.assertEqual(out[0]["c"], rows[0]["c"])
        self.assertEqual(out[-1]["c"], "0.5")
        self.assertEqual(len(api.calls), 2)                            # head (not backfilled) + tail
        store.backfilled = True
        api.calls.clear()
        f.candles("BTC", "1d", 0, rows[-1]["t"])
        self.assertEqual(api.calls, [(stored[-1]["t"] + DAY, rows[-1]["t"])])
        self.assertEqual(f.source_notes["BTC"]["stored"]["bars"], len(stored))
        self.assertEqual(f.source_notes["BTC"]["api"]["bars"], len(tail))
        meta = StrategyMeta(markets=("BTC",), timeframe="1d", lookback=50, max_leverage=1.0)
        data = fetch_market_data(meta, f, now_ms=rows[-1]["t"] + 2 * DAY, include_funding=False)
        notes = data.notes["coins"]["BTC"]
        self.assertEqual((notes["first_t"], notes["last_t"]), (rows[0]["t"], rows[-1]["t"]))
        self.assertEqual(notes["sources"]["stored"]["first_t"], rows[0]["t"])
        self.assertEqual(data.notes["data_range"], {"first_t": rows[0]["t"], "last_t": rows[-1]["t"]})
        self.assertEqual(Decimal(str(data.candles["BTC"][0][1])), Decimal(rows[0]["o"]))


if __name__ == "__main__":
    unittest.main()
