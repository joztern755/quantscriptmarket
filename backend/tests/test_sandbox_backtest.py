"""Backtest: simulation math (fees, funding sign, next-open execution, liquidation), metrics, data
fetching/pagination with an injected fetcher, and an end-to-end run through the sandbox runner.

Set SANDBOX_LIVE_TESTS=1 to also run one read-only backtest against the real Hyperliquid info API."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error

from app.errors import ExternalServiceError, ValidationFailed
from app.sandbox.backtest import (
    UNPROVEN_WARNING, BacktestParams, HyperliquidInfoFetcher, MarketData, backtest_on_data, compute_metrics,
    fetch_market_data, run_backtest, simulate,
)
from app.sandbox.validate import StrategyMeta

DAY = 86_400_000
HOUR = 3_600_000
P = BacktestParams()
FEE = (P.taker_fee_bps + P.builder_fee_bps) / 10_000
EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "examples", "strategies")


def flat_bars(prices, *, lows=None, highs=None, opens=None):
    rows = []
    for i, p in enumerate(prices):
        o = opens[i] if opens else p
        rows.append([i * DAY, o, highs[i] if highs else max(o, p), lows[i] if lows else min(o, p), p, 1.0])
    return rows


def sim(prices, weights, *, start=0, funding=None, params=P, **kw):
    rows = flat_bars(prices, **kw)
    ts = [r[0] for r in rows]
    w = {i: {"BTC": weights[i]} for i in range(len(prices))} if isinstance(weights, list) else weights
    return simulate(ts, {"BTC": rows}, w, interval_ms=DAY, markets=["BTC"], start_index=start, params=params,
                    funding=funding)


class SimulationTests(unittest.TestCase):
    def test_fees_on_entry(self):
        r = sim([100.0] * 3, [1.0, 1.0, 1.0])
        # enter at bar 1 open: notional 10_000 × (4.5 + 10) bps
        self.assertAlmostEqual(r.curve[1][1], 10_000 - 10_000 * FEE)
        self.assertAlmostEqual(r.fees_paid, 10_000 * FEE)
        self.assertEqual(len(r.trades), 1)
        self.assertAlmostEqual(r.curve[2][1], r.curve[1][1])  # no churn: already at target

    def test_executes_at_next_open_no_lookahead(self):
        # bar 1 jumps 100 → 200 intrabar. A signal decided after bar 1's close enters at bar 2's open (200)
        prices = [100.0, 200.0, 200.0, 220.0]
        opens = [100.0, 100.0, 200.0, 200.0]
        r = sim(prices, [0.0, 1.0, 1.0, 1.0], opens=opens)
        self.assertAlmostEqual(r.curve[1][1], 10_000.0)                      # bar 1: flat, missed the jump
        self.assertEqual(r.trades[0]["t"], 2 * DAY)
        self.assertEqual(r.trades[0]["price"], 200.0)
        # 50 units bought at 200; at bar 3's open the <2% top-up is skipped (churn rule); 200 → 220
        self.assertAlmostEqual(r.curve[3][1], 10_000 - 10_000 * FEE + 50 * 20.0, places=6)
        self.assertEqual(len(r.trades), 1)

    def test_short_pnl(self):
        r = sim([100.0, 100.0, 90.0], [-1.0, -1.0, -1.0], opens=[100.0, 100.0, 100.0])
        e = 10_000 * (1 - FEE)
        self.assertAlmostEqual(r.curve[2][1], e + 100 * 10.0, places=6)  # 100 units short, −10 each

    def test_funding_sign(self):
        fund = {"BTC": [[DAY + h * HOUR + 7, 0.0001] for h in range(24)]}  # during bar 1
        long_ = sim([100.0] * 2, [1.0, 1.0], funding=fund)
        short = sim([100.0] * 2, [-1.0, -1.0], funding=fund)
        notional = 10_000 * (1 - FEE)  # approx units × price after entry (units = 100)
        self.assertAlmostEqual(long_.funding_paid, 100 * 100 * 0.0001 * 24)
        self.assertAlmostEqual(short.funding_paid, -100 * 100 * 0.0001 * 24)
        self.assertAlmostEqual(long_.curve[1][1], 10_000 - 10_000 * FEE - 24.0)
        self.assertAlmostEqual(short.curve[1][1], 10_000 - 10_000 * FEE + 24.0)
        self.assertEqual(long_.funding_hours_covered, 24)
        self.assertGreater(notional, 0)

    def test_funding_ignored_when_flat(self):
        fund = {"BTC": [[DAY + h * HOUR, 0.01] for h in range(24)]}
        r = sim([100.0] * 2, [0.0, 0.0], funding=fund)
        self.assertEqual(r.funding_paid, 0.0)
        self.assertEqual(r.curve[-1][1], 10_000.0)

    def test_liquidation(self):
        prices = [100.0, 100.0, 100.0, 100.0]
        lows = [100.0, 100.0, 80.0, 100.0]
        params = BacktestParams(maintenance_margin_frac=0.03)
        r = sim(prices, [5.0] * 4, lows=lows, params=params)
        self.assertEqual(r.liquidated_at, 2 * DAY)
        self.assertEqual(r.curve[-1][1], 0.0)
        self.assertEqual(r.curve[-2][1], 0.0)
        # 1x long survives the same wick
        self.assertIsNone(sim(prices, [1.0] * 4, lows=lows, params=params).liquidated_at)

    def test_churn_rule_and_flat_always_executes(self):
        r = sim([100.0] * 4, [1.0, 1.01, 0.0, 0.0])
        sides = [(t["t"] // DAY, t["side"]) for t in r.trades]
        self.assertEqual(sides, [(1, "buy"), (3, "sell")])  # 1% tweak skipped; exit to flat executed

    def test_lookahead_guard(self):
        rows = flat_bars([100.0] * 3)
        ts = [r[0] for r in rows]
        with self.assertRaises(AssertionError):
            simulate(ts, {"BTC": rows}, {0: {"BTC": 1.0}, 1: {"BTC": 1.0}}, interval_ms=DAY, markets=["BTC"],
                     start_index=0, params=P, signal_times={0: ts[1], 1: ts[2]})

    def test_missing_signal(self):
        with self.assertRaises(ValidationFailed):
            sim([100.0] * 3, {0: {"BTC": 1.0}})


class MetricTests(unittest.TestCase):
    def test_basic(self):
        curve = [(0, 100.0), (DAY, 110.0), (2 * DAY, 99.0)]
        m = compute_metrics(curve, [1.0, 0.0], DAY)
        self.assertAlmostEqual(m["total_return"], -0.01)
        self.assertAlmostEqual(m["max_drawdown"], 0.1)
        self.assertAlmostEqual(m["exposure"], 0.5)
        self.assertIsNone(m["cagr"])  # < 30 days: not annualised
        self.assertIsNotNone(m["sharpe"])

    def test_cagr(self):
        curve = [(0, 100.0), (int(365.25 * DAY), 121.0), (int(2 * 365.25 * DAY), 121.0 * 1.21 / 1.1)]
        m = compute_metrics(curve[:2], [1.0], DAY)
        self.assertAlmostEqual(m["cagr"], 0.21)

    def test_wiped_out(self):
        m = compute_metrics([(0, 100.0), (400 * DAY, 0.0)], [1.0], DAY)
        self.assertEqual(m["cagr"], -1.0)
        self.assertEqual(m["max_drawdown"], 1.0)


class FakeFetcher:
    def __init__(self, candles, funding=None):
        self._c, self._f = candles, funding or {}
        self.calls = []

    def candles(self, coin, interval, start_ms, end_ms):
        self.calls.append(("candles", coin, interval, start_ms, end_ms))
        return [c for c in self._c[coin] if start_ms <= c["t"] <= end_ms]

    def funding(self, coin, start_ms, end_ms):
        self.calls.append(("funding", coin, start_ms, end_ms))
        return [f for f in self._f.get(coin, []) if start_ms <= f["time"] <= end_ms]


def hl_candles(prices, coin="BTC", zero_lead=0, t0=0):
    out = []
    for i, p in enumerate(prices):
        z = i < zero_lead
        out.append({"t": t0 + i * DAY, "T": t0 + (i + 1) * DAY - 1, "s": coin, "i": "1d", "o": str(p), "c": str(p),
                    "h": str(p * 1.01), "l": str(p * 0.99), "v": "0.0" if z else "12.5", "n": 0 if z else 42})
    return out


def trend_prices(n):
    return [100.0 * (1.001 ** i) * (1 + 0.05 * ((i // 40) % 2)) for i in range(n)]


class DataTests(unittest.TestCase):
    meta = StrategyMeta(("BTC",), "1d", 50, 1.0)

    def test_drops_forming_bar_and_prelisting(self):
        prices = trend_prices(100)
        f = FakeFetcher({"BTC": hl_candles(prices, zero_lead=10)},
                        {"BTC": [{"coin": "BTC", "fundingRate": "0.0000125", "premium": "0", "time": 20 * DAY + 5}]})
        now = 99 * DAY + 5  # bar 99 is still forming
        d = fetch_market_data(self.meta, f, now_ms=now)
        rows = d.candles["BTC"]
        self.assertEqual(rows[0][0], 10 * DAY)
        self.assertEqual(rows[-1][0], 98 * DAY)
        self.assertEqual(d.notes["coins"]["BTC"]["dropped_leading_zero_volume"], 10)
        self.assertEqual(d.funding["BTC"], [[20 * DAY + 5, 0.0000125]])
        self.assertTrue(all(c[3] <= now for c in f.calls if c[0] == "candles"))

    def test_marketdata_roundtrip_and_rejects(self):
        d = MarketData("1d", {"BTC": flat_bars([1.0, 2.0])}, {"BTC": [[1, 0.0001]]})
        d2 = MarketData.from_json(json.loads(json.dumps(d.to_json())))
        self.assertEqual(d2.candles, d.candles)
        with self.assertRaises(ValidationFailed):
            MarketData.from_json({"timeframe": "1d", "candles": {"BTC": [[0, 1, 1, 1, float("nan"), 1]]}})
        with self.assertRaises(ValidationFailed):
            MarketData.from_json({"timeframe": "1d", "candles": {"BTC": flat_bars([1.0])}, "funding": {"BTC": [[0, 5.0]]}})
        with self.assertRaises(ValidationFailed):
            MarketData.from_json({"timeframe": "7d", "candles": {}})

    def test_params(self):
        self.assertEqual(BacktestParams.from_dict({"taker_fee_bps": 3.5}).taker_fee_bps, 3.5)
        for bad in ({"nope": 1}, {"taker_fee_bps": "x"}, {"taker_fee_bps": -1}, {"initial_equity": float("inf")}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationFailed):
                    BacktestParams.from_dict(bad)


class FakeHTTP:
    """Stands in for urllib: serves candleSnapshot (≤5000/page) and fundingHistory (≤500/page)."""

    def __init__(self, n_candles, n_funding, fail_first=0):
        self.candles = [{"t": i * HOUR, "o": "1", "h": "1", "l": "1", "c": "1", "v": "1", "n": 1} for i in range(n_candles)]
        self.funding = [{"coin": "BTC", "fundingRate": "0.00001", "premium": "0", "time": i * HOUR + 3} for i in range(n_funding)]
        self.fail_first = fail_first
        self.urls, self.bodies = [], []

    def __call__(self, req, timeout):
        self.urls.append(req.full_url)
        body = json.loads(req.data)
        self.bodies.append(body)
        if self.fail_first:
            self.fail_first -= 1
            raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, io.BytesIO(b""))
        if body["type"] == "candleSnapshot":
            q = body["req"]
            page = [c for c in self.candles if q["startTime"] <= c["t"] <= q["endTime"]][:5000]
        else:
            page = [f for f in self.funding if body["startTime"] <= f["time"] <= body.get("endTime", 1 << 62)][:500]
        return json.dumps(page).encode()


class FetcherTests(unittest.TestCase):
    def make(self, http):
        return HyperliquidInfoFetcher(opener=http, sleep=lambda s: None, min_interval_s=0)

    def test_candle_pagination(self):
        http = FakeHTTP(12_345, 0)
        got = self.make(http).candles("BTC", "1h", 0, 20_000 * HOUR)
        self.assertEqual(len(got), 12_345)
        self.assertEqual([c["t"] for c in got], sorted({c["t"] for c in got}))
        self.assertEqual(len(http.bodies), 3)
        self.assertTrue(all(u == "https://api.hyperliquid.xyz/info" for u in http.urls))

    def test_funding_pagination(self):
        http = FakeHTTP(0, 1_234)
        got = self.make(http).funding("BTC", 0, 10_000 * HOUR)
        self.assertEqual(len(got), 1_234)
        self.assertEqual(len(http.bodies), 3)
        self.assertEqual(http.bodies[1]["startTime"], got[499]["time"] + 1)

    def test_retry_then_fail(self):
        http = FakeHTTP(10, 0, fail_first=2)
        self.assertEqual(len(self.make(http).candles("BTC", "1h", 0, 100 * HOUR)), 10)
        http = FakeHTTP(10, 0, fail_first=99)
        f = HyperliquidInfoFetcher(opener=http, sleep=lambda s: None, min_interval_s=0, max_retries=2)
        with self.assertRaises(ExternalServiceError):
            f.candles("BTC", "1h", 0, 100 * HOUR)

    def test_never_exchange(self):
        f = HyperliquidInfoFetcher("https://api.hyperliquid.xyz/")
        self.assertTrue(f.url.endswith("/info"))
        self.assertNotIn("exchange", f.url)


class EndToEndTests(unittest.TestCase):
    def test_example_script_backtest(self):
        with open(os.path.join(EXAMPLES, "sma_trend_btc.py"), encoding="utf-8") as fh:
            src = fh.read()
        prices = trend_prices(700)
        fund = [{"coin": "BTC", "fundingRate": "0.00001", "premium": "0", "time": i * HOUR + 1} for i in range(0, 700 * 24, 1)]
        f = FakeFetcher({"BTC": hl_candles(prices, zero_lead=5)}, {"BTC": fund})
        rep = run_backtest(src, fetcher=f, now_ms=700 * DAY)
        self.assertIn(UNPROVEN_WARNING, rep["warnings"])
        self.assertEqual(rep["period"]["first_bar_t"], 5 * DAY)
        self.assertEqual(rep["period"]["first_signal_t"], (5 + 249) * DAY)
        m = rep["metrics"]
        self.assertEqual(m["full"]["bars"], m["in_sample"]["bars"] + m["out_of_sample"]["bars"])
        self.assertAlmostEqual(m["in_sample"]["bars"] / m["full"]["bars"], 0.7, delta=0.01)
        self.assertEqual(len(rep["equity_curve"]), m["full"]["bars"] + 1)
        self.assertGreater(rep["trade_count"], 0)
        self.assertGreater(rep["funding_paid"], 0)  # net long with positive funding pays
        self.assertEqual(rep["buy_and_hold"]["full"]["exposure"], 1.0)
        self.assertEqual(set(rep["latest_signal"]["weights"]), {"BTC"})
        # ≥1y listing rule: bars 5..699, first signal at 254 → 445 simulated days
        self.assertAlmostEqual(rep["period"]["sim_days"], 445.0)
        self.assertTrue(rep["listing_eligible_history"])
        json.dumps(rep, allow_nan=False)  # report must be strict-JSON serialisable

    def test_multi_market_alignment_and_bh(self):
        src = ('MARKETS = ["BTC", "SOL"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 1\n'
               'def signal(bars):\n    return {"BTC": 0.5, "SOL": 0.5}\n')
        btc = flat_bars(trend_prices(200))
        sol = [r for r in flat_bars([p * 0.1 for p in trend_prices(200)]) if r[0] >= 30 * DAY]
        rep = backtest_on_data(src, MarketData("1d", {"BTC": btc, "SOL": sol}))
        self.assertEqual(rep["period"]["first_bar_t"], 30 * DAY)
        self.assertTrue(any("aligned" in w or "align" in w for w in rep["warnings"]))
        # constant 50/50 == buy & hold 50/50 → identical curves
        self.assertEqual(rep["equity_curve"], rep["buy_and_hold_curve"])

    def test_script_error_surfaces(self):
        src = 'MARKETS = ["BTC"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 1\ndef signal(bars):\n    return {"BTC": 1 / 0}\n'
        with self.assertRaises(ValidationFailed) as cm:
            backtest_on_data(src, MarketData("1d", {"BTC": flat_bars(trend_prices(80))}))
        self.assertEqual(cm.exception.details["kind"], "exception")

    def test_not_enough_history(self):
        src = 'MARKETS = ["BTC"]\nTIMEFRAME = "1d"\nLOOKBACK = 50\nMAX_LEVERAGE = 1\ndef signal(bars):\n    return {}\n'
        with self.assertRaises(ValidationFailed):
            backtest_on_data(src, MarketData("1d", {"BTC": flat_bars(trend_prices(40))}))

    def test_timeframe_mismatch(self):
        src = 'MARKETS = ["BTC"]\nTIMEFRAME = "4h"\nLOOKBACK = 50\nMAX_LEVERAGE = 1\ndef signal(bars):\n    return {}\n'
        with self.assertRaises(ValidationFailed):
            backtest_on_data(src, MarketData("1d", {"BTC": flat_bars(trend_prices(80))}))


@unittest.skipUnless(os.environ.get("SANDBOX_LIVE_TESTS") == "1", "set SANDBOX_LIVE_TESTS=1 for the live info-API test")
class LiveTests(unittest.TestCase):
    def test_live_btc_daily(self):
        with open(os.path.join(EXAMPLES, "sma_trend_btc.py"), encoding="utf-8") as fh:
            src = fh.read()
        rep = run_backtest(src, fetcher=HyperliquidInfoFetcher(min_interval_s=0.5))
        self.assertGreater(rep["period"]["aligned_bars"], 1000)
        self.assertTrue(rep["listing_eligible_history"])


if __name__ == "__main__":
    unittest.main()
