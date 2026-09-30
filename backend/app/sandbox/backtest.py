"""Walk-forward style backtest for uploaded strategy scripts (SPEC §10).

Pipeline::

    fetch_market_data(meta, HyperliquidInfoFetcher())      # trusted side (api) — has egress
      → MarketData (JSON-serialisable, passed to the sandbox service which has NO egress)
    backtest_on_data(source, data, params)                 # runs the script via runner.run_series
      → report dict (equity curve, trades, metrics full / in-sample / out-of-sample, buy & hold)

``run_backtest(source, fetcher=...)`` does both in one process (tests, local dev).

Model (all approximations are listed in the report's ``warnings``):

* Signal at bar ``i`` is computed from bars ``≤ i`` only (the runner slices; the child asserts; this
  module re-asserts ``signal.t < execution bar t``). Target position = ``weight × equity`` per coin,
  rebalanced at the **next bar's open** with taker fee (default 4.5 bps — UNVERIFIED base-tier rate;
  HIP-3 markets may differ) + builder fee 10 bps on traded notional. Rebalances smaller than
  ``max(min_order_usd, min_rebalance_frac × equity)`` are skipped (mirrors the live churn rule, SPEC §10)
  except moves to flat, which always execute.
* Funding from ``fundingHistory`` (hourly rates). Sign: positive rate ⇒ longs pay, shorts receive;
  payment = −units × price × rate, price approximated by the bar close. Hours without funding data are
  treated as zero (reported as coverage).
* Liquidation APPROXIMATED: per bar, if equity at the simultaneous worst intrabar price of every open
  position (low for longs, high for shorts) falls to ≤ maintenance margin
  (``maintenance_margin_frac`` × notional at that price; default 3 %, real HL values differ per asset),
  the account is treated as liquidated and equity set to 0 (conservative: total loss of allocation).
* Candles: Hyperliquid serves only the most recent 5000 candles per coin/interval (verified 2026-09-30:
  ``candleSnapshot`` for BTC 1h returned exactly 5001 bars regardless of ``startTime`` and nothing
  before them), so 1h history is ≈208 days and 4h ≈833 days. Leading zero-volume bars (pre-listing
  reference prices, e.g. BTC 1d before 2023-02-26) are dropped because nothing could trade on them.
* In-sample = first 70 % of simulated bars, out-of-sample = last 30 % (same continuous run, rebased).
  Nothing is re-fitted, so this is **not** a true walk-forward: the SPEC warning is always attached.
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from app.errors import ExternalServiceError, ValidationFailed
from app.sandbox.runner import SERIES_LIMITS, RunLimits, SeriesResult, normalize_bars, run_series, with_limits
from app.sandbox.validate import TIMEFRAME_MS, StrategyMeta, ensure_valid

__all__ = [
    "BacktestParams", "MarketData", "CandleFetcher", "HyperliquidInfoFetcher", "fetch_market_data",
    "align_bars", "simulate", "compute_metrics", "backtest_on_data", "run_backtest",
    "UNPROVEN_WARNING", "HL_MAX_CANDLES",
]

DAY_MS = 86_400_000
YEAR_MS = 365.25 * DAY_MS
HOUR_MS = 3_600_000
HL_MAX_CANDLES = 5000
UNPROVEN_WARNING = "Backtest of a newly uploaded script can be fitted to history; not proven live yet"


@dataclass(frozen=True)
class BacktestParams:
    initial_equity: float = 10_000.0
    taker_fee_bps: float = 4.5            # UNVERIFIED: Hyperliquid base-tier perp taker 0.045%
    builder_fee_bps: float = 10.0         # SPEC §1: 0.1% builder fee on every strategy order
    maintenance_margin_frac: float = 0.03  # APPROXIMATION; HL maintenance = 1/(2 × asset max leverage)
    min_order_usd: float = 10.0           # SPEC §10 churn rule
    min_rebalance_frac: float = 0.02      # SPEC §10 churn rule (2% of allocation)
    in_sample_frac: float = 0.70
    min_history_days_to_list: int = 365   # SPEC §10: ≥ 1 year required to list
    cpu_budget_seconds: int = 120         # total CPU for the whole series run in the sandbox child
    max_trades_reported: int = 20_000

    def validate(self) -> None:
        checks = [
            (self.initial_equity > 0, "initial_equity must be > 0"),
            (0 <= self.taker_fee_bps <= 100, "taker_fee_bps must be 0–100"),
            (0 <= self.builder_fee_bps <= 100, "builder_fee_bps must be 0–100"),
            (0 <= self.maintenance_margin_frac < 1, "maintenance_margin_frac must be 0–1"),
            (0.1 <= self.in_sample_frac <= 0.9, "in_sample_frac must be 0.1–0.9"),
            (1 <= self.cpu_budget_seconds <= 600, "cpu_budget_seconds must be 1–600"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ValidationFailed(msg)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "BacktestParams":
        if not d:
            return cls()
        allowed = set(cls.__dataclass_fields__)
        unknown = set(d) - allowed
        if unknown:
            raise ValidationFailed(f"unknown backtest params: {sorted(unknown)}")
        kw = {}
        for k, v in d.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValidationFailed(f"param {k} must be a finite number")
            kw[k] = type(getattr(cls(), k))(v)
        p = cls(**kw)
        p.validate()
        return p


@dataclass
class MarketData:
    """Everything the simulation needs; JSON-serialisable so the api can hand it to the sandbox."""
    timeframe: str
    candles: dict[str, list[list[float]]]          # coin -> [[t, o, h, l, c, v], ...] closed bars only
    funding: dict[str, list[list[float]]] = field(default_factory=dict)  # coin -> [[time_ms, rate], ...]
    notes: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"timeframe": self.timeframe, "candles": self.candles, "funding": self.funding, "notes": self.notes}

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> "MarketData":
        if not isinstance(d, Mapping) or d.get("timeframe") not in TIMEFRAME_MS:
            raise ValidationFailed("data.timeframe missing or invalid")
        candles = d.get("candles")
        if not isinstance(candles, Mapping) or not candles:
            raise ValidationFailed("data.candles missing")
        norm = normalize_bars(candles)
        funding: dict[str, list[list[float]]] = {}
        for coin, evs in (d.get("funding") or {}).items():
            out = []
            for ev in evs:
                t, r = ev
                r = float(r)
                if not math.isfinite(r) or abs(r) > 1:
                    raise ValidationFailed(f"bad funding rate for {coin}")
                out.append([int(t), r])
            out.sort()
            funding[str(coin)] = out
        return cls(timeframe=d["timeframe"], candles=norm, funding=funding, notes=dict(d.get("notes") or {}))


# ---------------------------------------------------------------------------------------------------
# Fetching (trusted side only — the sandbox service has no egress)
# ---------------------------------------------------------------------------------------------------

class CandleFetcher(Protocol):
    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]: ...
    def funding(self, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]: ...


class HyperliquidInfoFetcher:
    """Read-only client for ``POST {base}/info``. Never touches ``/exchange``.

    Pagination: candles forward in windows (≤ 5000 bars per response; only the latest 5000 bars per
    coin/interval exist at all); funding 500 events per response, cursor = last time + 1.
    Throttled (``min_interval_s``) with exponential backoff on 429/5xx — info-API weight limits are
    UNVERIFIED here (SPEC §6), keep usage gentle and cache results upstream."""

    FUNDING_PAGE = 500

    def __init__(self, base_url: str = "https://api.hyperliquid.xyz", *, timeout: float = 20.0,
                 min_interval_s: float = 0.5, max_retries: int = 4,
                 opener: Callable[[urllib.request.Request, float], bytes] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.url = base_url.rstrip("/") + "/info"
        assert self.url.endswith("/info") and "/exchange" not in self.url
        self.timeout, self.min_interval_s, self.max_retries = timeout, min_interval_s, max_retries
        self._opener = opener or self._default_open
        self._sleep, self._clock = sleep, clock
        self._last = 0.0
        self.calls = 0

    @staticmethod
    def _default_open(req: urllib.request.Request, timeout: float) -> bytes:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https URL)
            return r.read(64 * 1024 * 1024)

    def _post(self, body: dict[str, Any]) -> Any:
        data = json.dumps(body).encode()
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            wait = self.min_interval_s - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            self.calls += 1
            req = urllib.request.Request(self.url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                return json.loads(self._opener(req, self.timeout))
            except urllib.error.HTTPError as e:
                retry = e.code == 429 or e.code >= 500
                err: Exception = e
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                retry, err = True, e
            if not retry or attempt == self.max_retries:
                raise ExternalServiceError(f"hyperliquid info {body.get('type')} failed: {err}")
            self._sleep(delay)
            delay *= 2
        raise ExternalServiceError("unreachable")  # pragma: no cover

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        step = TIMEFRAME_MS[interval]
        out: dict[int, dict[str, Any]] = {}
        cursor = start_ms
        for _ in range(100):
            page = self._post({"type": "candleSnapshot",
                               "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms}})
            if not isinstance(page, list) or not page:
                break
            for c in page:
                out[int(c["t"])] = c
            last = max(int(c["t"]) for c in page)
            nxt = last + step
            if nxt <= cursor or nxt > end_ms or len(page) < HL_MAX_CANDLES:
                break
            cursor = nxt
        return [out[t] for t in sorted(out)]

    def funding(self, coin: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        cursor = start_ms
        for _ in range(2000):
            page = self._post({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms})
            if not isinstance(page, list) or not page:
                break
            for ev in page:
                out[int(ev["time"])] = ev
            last = max(int(ev["time"]) for ev in page)
            if len(page) < self.FUNDING_PAGE or last + 1 > end_ms or last + 1 <= cursor:
                break
            cursor = last + 1
        return [out[t] for t in sorted(out)]


def fetch_market_data(meta: StrategyMeta, fetcher: CandleFetcher, *, start_ms: int | None = None,
                      end_ms: int | None = None, now_ms: int | None = None,
                      include_funding: bool = True) -> MarketData:
    """Fetch closed candles (+ funding) for every market in ``meta``. Drops the still-forming bar and
    leading zero-volume (pre-listing) bars."""
    now = int(time.time() * 1000) if now_ms is None else now_ms
    end = min(end_ms if end_ms is not None else now, now)
    start = start_ms if start_ms is not None else 0
    step = meta.interval_ms
    candles: dict[str, list[list[float]]] = {}
    funding: dict[str, list[list[float]]] = {}
    notes: dict[str, Any] = {"fetched_at_ms": now, "coins": {}}
    for coin in meta.markets:
        raw = fetcher.candles(coin, meta.timeframe, start, end)
        rows = []
        for c in raw:
            t = int(c["t"])
            if t + step > now or t > end:
                continue  # still forming (or beyond range): never hand it to a script
            rows.append({"t": t, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v", "0"), "n": c.get("n")})
        lead = 0
        while lead < len(rows) and (rows[lead]["n"] == 0 if rows[lead]["n"] is not None else float(rows[lead]["v"]) == 0):
            lead += 1
        rows = rows[lead:]
        candles[coin] = normalize_bars({coin: rows})[coin]
        notes["coins"][coin] = {"bars": len(rows), "dropped_leading_zero_volume": lead,
                                "hit_hl_candle_cap": len(raw) >= HL_MAX_CANDLES}
        if include_funding and rows:
            evs = fetcher.funding(coin, int(rows[0]["t"]), end + step)
            funding[coin] = [[int(e["time"]), float(e["fundingRate"])] for e in evs]
    return MarketData(timeframe=meta.timeframe, candles=candles, funding=funding, notes=notes)


def align_bars(candles: Mapping[str, Sequence[Sequence[float]]], markets: Sequence[str]) -> tuple[list[int], dict[str, list[list[float]]]]:
    """Keep only timestamps present for every market (intersection), preserving order."""
    missing = [c for c in markets if not candles.get(c)]
    if missing:
        raise ValidationFailed(f"no candles for {', '.join(missing)}")
    common = set(int(r[0]) for r in candles[markets[0]])
    for c in markets[1:]:
        common &= set(int(r[0]) for r in candles[c])
    ts = sorted(common)
    aligned = {c: [list(r) for r in candles[c] if int(r[0]) in common] for c in markets}
    return ts, aligned


# ---------------------------------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------------------------------

@dataclass
class SimResult:
    curve: list[tuple[int, float]]           # (bar close ms, equity) — first point = start equity
    exposure: list[float]                    # gross leverage held during each simulated bar
    trades: list[dict[str, Any]]
    fees_paid: float
    funding_paid: float                      # positive = paid by the account
    turnover: float
    liquidated_at: int | None
    funding_hours_covered: int
    funding_hours_total: int


def simulate(ts: Sequence[int], bars: Mapping[str, Sequence[Sequence[float]]], weights_at: Mapping[int, Mapping[str, float]],
             *, interval_ms: int, markets: Sequence[str], start_index: int, params: BacktestParams,
             funding: Mapping[str, Sequence[Sequence[float]]] | None = None,
             signal_times: Mapping[int, int] | None = None) -> SimResult:
    """Simulate from bar ``start_index`` (equity measured at its close) to the last bar.

    ``weights_at[i]`` = weights decided after bar ``i`` closed; executed at bar ``i + 1``'s open."""
    n = len(ts)
    fee_rate = (params.taker_fee_bps + params.builder_fee_bps) / 10_000.0
    cash = params.initial_equity
    units = {c: 0.0 for c in markets}
    curve: list[tuple[int, float]] = [(ts[start_index] + interval_ms, cash)]
    exposure: list[float] = []
    trades: list[dict[str, Any]] = []
    fees = fund_paid = turnover = 0.0
    liquidated_at: int | None = None
    fund_times = {c: [int(e[0]) for e in (funding or {}).get(c, [])] for c in markets}
    fund_rates = {c: [float(e[1]) for e in (funding or {}).get(c, [])] for c in markets}
    hours_cov = hours_tot = 0

    for j in range(start_index + 1, n):
        t_open = ts[j]
        row = {c: bars[c][j] for c in markets}
        if liquidated_at is not None:
            curve.append((t_open + interval_ms, 0.0)); exposure.append(0.0)
            continue
        w = weights_at.get(j - 1)
        if w is None:
            raise ValidationFailed(f"missing signal for bar index {j - 1}")
        if signal_times is not None:
            # look-ahead guard: the decision used only bars that closed before this execution bar opened
            assert signal_times[j - 1] + interval_ms <= t_open, "look-ahead: signal newer than execution bar"
        # 1) rebalance at the open
        eq_open = cash + sum(units[c] * row[c][1] for c in markets)
        if eq_open > 0:
            for c in markets:
                px = row[c][1]
                target = w.get(c, 0.0) * eq_open / px
                d = target - units[c]
                notional = abs(d) * px
                if notional == 0:
                    continue
                to_flat = target == 0.0
                if not to_flat and notional < max(params.min_order_usd, params.min_rebalance_frac * eq_open):
                    continue
                fee = notional * fee_rate
                cash -= d * px + fee
                units[c] = 0.0 if to_flat else target
                fees += fee
                turnover += notional
                if len(trades) < params.max_trades_reported:
                    trades.append({"t": t_open, "coin": c, "side": "buy" if d > 0 else "sell", "size": abs(d),
                                   "price": px, "notional": notional, "fee": fee,
                                   "target_weight": w.get(c, 0.0)})
        # 2) funding paid/received during the bar (hourly events in [t_open, t_open + interval))
        for c in markets:
            ft = fund_times[c]
            lo = bisect.bisect_left(ft, t_open)
            hi = bisect.bisect_left(ft, t_open + interval_ms)
            if units[c] != 0.0:
                hours_tot += max(1, interval_ms // HOUR_MS)
                hours_cov += hi - lo
                px = row[c][4]
                for k in range(lo, hi):
                    pay = units[c] * px * fund_rates[c][k]   # >0 for longs when rate > 0
                    cash -= pay
                    fund_paid += pay
        # 3) liquidation check at the simultaneous worst intrabar prices (approximation)
        if any(units[c] != 0.0 for c in markets):
            worst = cash
            maint = 0.0
            for c in markets:
                adverse = row[c][3] if units[c] > 0 else row[c][2]
                worst += units[c] * adverse
                maint += abs(units[c]) * adverse * params.maintenance_margin_frac
            if worst <= maint:
                liquidated_at = t_open
                units = {c: 0.0 for c in markets}
                cash = 0.0
                trades.append({"t": t_open, "coin": "*", "side": "liquidation", "size": 0.0, "price": 0.0,
                               "notional": 0.0, "fee": 0.0, "target_weight": 0.0})
        # 4) mark to market at the close
        eq_close = cash + sum(units[c] * row[c][4] for c in markets)
        gross = sum(abs(units[c]) * row[c][4] for c in markets)
        exposure.append(gross / eq_close if eq_close > 0 else 0.0)
        if eq_close <= 0 and liquidated_at is None:
            liquidated_at = t_open
            units = {c: 0.0 for c in markets}
            cash = eq_close = 0.0
        curve.append((t_open + interval_ms, max(eq_close, 0.0)))
    return SimResult(curve, exposure, trades, fees, fund_paid, turnover, liquidated_at, hours_cov, hours_tot)


def compute_metrics(curve: Sequence[tuple[int, float]], exposure: Sequence[float], interval_ms: int) -> dict[str, Any]:
    """Metrics for an equity curve (first point = starting equity). ``exposure[k]`` is for the bar ending
    at ``curve[k + 1]``."""
    if len(curve) < 2:
        return {"bars": 0, "total_return": 0.0, "cagr": None, "max_drawdown": 0.0, "sharpe": None,
                "exposure": 0.0, "avg_gross_leverage": 0.0, "start_t": curve[0][0] if curve else None,
                "end_t": curve[-1][0] if curve else None}
    e0, e1 = curve[0][1], curve[-1][1]
    total = e1 / e0 - 1.0 if e0 > 0 else 0.0
    years = (curve[-1][0] - curve[0][0]) / YEAR_MS
    cagr = None
    if years >= 30 / 365.25 and e0 > 0:
        try:
            cagr = (e1 / e0) ** (1.0 / years) - 1.0 if e1 > 0 else -1.0
        except OverflowError:
            cagr = None
    peak, mdd = curve[0][1], 0.0
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    rets = []
    for (_, a), (_, b) in zip(curve, curve[1:]):
        rets.append(b / a - 1.0 if a > 0 else 0.0)
    sharpe = None
    if len(rets) >= 2:
        sd = statistics.pstdev(rets)
        if sd > 0:
            sharpe = statistics.fmean(rets) / sd * math.sqrt(YEAR_MS / interval_ms)
    exp = list(exposure)
    return {
        "bars": len(curve) - 1, "start_t": curve[0][0], "end_t": curve[-1][0], "years": years,
        "total_return": total, "cagr": cagr, "max_drawdown": mdd, "sharpe": sharpe,
        "exposure": (sum(1 for x in exp if x > 1e-12) / len(exp)) if exp else 0.0,
        "avg_gross_leverage": statistics.fmean(exp) if exp else 0.0,
        "final_equity": e1,
    }


def _segments(sim: SimResult, interval_ms: int, is_frac: float) -> dict[str, Any]:
    nbars = len(sim.curve) - 1
    k = max(1, min(nbars - 1, int(round(nbars * is_frac)))) if nbars >= 2 else nbars
    return {
        "full": compute_metrics(sim.curve, sim.exposure, interval_ms),
        "in_sample": compute_metrics(sim.curve[:k + 1], sim.exposure[:k], interval_ms),
        "out_of_sample": compute_metrics(sim.curve[k:], sim.exposure[k:], interval_ms),
        "split_t": sim.curve[k][0] if sim.curve else None,
    }


# ---------------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------------

SeriesRunner = Callable[..., SeriesResult]


def backtest_on_data(source: str, data: MarketData, *, params: BacktestParams | None = None,
                     meta: StrategyMeta | None = None, limits: RunLimits | None = None,
                     series_runner: SeriesRunner = run_series) -> dict[str, Any]:
    params = params or BacktestParams()
    params.validate()
    meta = meta or ensure_valid(source)
    if data.timeframe != meta.timeframe:
        raise ValidationFailed(f"data timeframe {data.timeframe} != script TIMEFRAME {meta.timeframe}")
    interval = meta.interval_ms
    ts, aligned = align_bars(data.candles, meta.markets)
    lens = {c: len(data.candles[c]) for c in meta.markets}
    start = meta.lookback - 1
    if len(ts) < meta.lookback + 2:
        raise ValidationFailed(f"not enough history: {len(ts)} aligned bars, need ≥ LOOKBACK + 2 = {meta.lookback + 2}")
    lim = limits or with_limits(SERIES_LIMITS, total_cpu_seconds=params.cpu_budget_seconds,
                                wall_timeout_seconds=params.cpu_budget_seconds * 2 + 30)
    series = series_runner(source, aligned, meta=meta, start_index=start, end_index=len(ts), limits=lim)
    weights_at = {s.index: s.weights for s in series.steps}
    sig_t = {s.index: s.t for s in series.steps}
    for s in series.steps:  # independent look-ahead check on what came back from the sandbox
        if s.t != ts[s.index]:
            raise ValidationFailed("look-ahead guard: step timestamp mismatch")
    sim = simulate(ts, aligned, weights_at, interval_ms=interval, markets=meta.markets, start_index=start,
                   params=params, funding=data.funding, signal_times=sig_t)
    bh_w = {c: 1.0 / len(meta.markets) for c in meta.markets}
    bh = simulate(ts, aligned, {i: bh_w for i in range(start, len(ts))}, interval_ms=interval,
                  markets=meta.markets, start_index=start, params=params, funding=data.funding)

    sim_days = (sim.curve[-1][0] - sim.curve[0][0]) / DAY_MS if len(sim.curve) > 1 else 0.0
    warnings = [
        UNPROVEN_WARNING,
        f"Taker fee {params.taker_fee_bps:g} bps is an UNVERIFIED base-tier assumption; builder-deployed (HIP-3) markets may charge more.",
        "Liquidation is approximated (worst intrabar prices, flat maintenance margin, total loss on liquidation).",
        "Funding uses hourly historical rates priced at the bar close; missing hours count as zero.",
        "Fills assumed at the bar open with no slippage beyond fees; live orders are IOC within 0.5% of mid.",
    ]
    if sim_days < params.min_history_days_to_list:
        warnings.append(f"Only {sim_days:.0f} days simulated; ≥ {params.min_history_days_to_list} days are required to list.")
    if meta.timeframe != "1d":
        warnings.append(f"Hyperliquid serves only the latest {HL_MAX_CANDLES} {meta.timeframe} candles, which limits history.")
    dropped = sum(lens.values()) - len(ts) * len(meta.markets)
    if dropped:
        warnings.append(f"{dropped} bars dropped to align timestamps across markets (history starts at the newest market's listing).")
    if sim.funding_hours_total and sim.funding_hours_covered < 0.9 * sim.funding_hours_total:
        warnings.append(f"Funding data covered only {sim.funding_hours_covered}/{sim.funding_hours_total} position-hours.")
    if sim.liquidated_at is not None:
        warnings.append("The strategy was LIQUIDATED in this backtest (approximate model).")

    last = series.steps[-1] if series.steps else None
    return {
        "meta": meta.to_dict(),
        "params": asdict(params),
        "period": {"first_bar_t": ts[0], "last_bar_t": ts[-1], "aligned_bars": len(ts),
                   "first_signal_t": ts[start], "sim_start_t": sim.curve[0][0], "sim_end_t": sim.curve[-1][0],
                   "sim_days": sim_days},
        "data_notes": data.notes,
        "equity_curve": [[t, e] for t, e in sim.curve],
        "buy_and_hold_curve": [[t, e] for t, e in bh.curve],
        "trades": sim.trades,
        "trade_count": sum(1 for t in sim.trades if t["side"] != "liquidation"),
        "fees_paid": sim.fees_paid,
        "funding_paid": sim.funding_paid,
        "turnover": sim.turnover,
        "funding_coverage": {"covered_hours": sim.funding_hours_covered, "position_hours": sim.funding_hours_total},
        "liquidated": sim.liquidated_at is not None,
        "liquidated_at": sim.liquidated_at,
        "metrics": _segments(sim, interval, params.in_sample_frac),
        "buy_and_hold": _segments(bh, interval, params.in_sample_frac),
        "latest_signal": {"bar_t": last.t, "weights": last.weights} if last else None,
        "listing_eligible_history": sim_days >= params.min_history_days_to_list,
        "cpu_seconds": series.cpu_seconds,
        "warnings": warnings,
    }


def run_backtest(source: str, *, fetcher: CandleFetcher, start_ms: int | None = None, end_ms: int | None = None,
                 now_ms: int | None = None, params: BacktestParams | None = None, limits: RunLimits | None = None,
                 series_runner: SeriesRunner = run_series, known_markets: Iterable[str] | None = None) -> dict[str, Any]:
    """Validate, fetch data with ``fetcher`` (injected; ``HyperliquidInfoFetcher`` in prod), and backtest."""
    meta = ensure_valid(source, known_markets=known_markets)
    data = fetch_market_data(meta, fetcher, start_ms=start_ms, end_ms=end_ms, now_ms=now_ms)
    return backtest_on_data(source, data, params=params, meta=meta, limits=limits, series_runner=series_runner)
