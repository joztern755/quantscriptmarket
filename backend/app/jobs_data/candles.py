"""Own candle history (SPEC §12 "Own candle history", "Listing history rule").

``candles_sync(db, now)`` — ``/internal/candles-sync``. For every LIVE perp market (validator dex + every builder dex in
``perpDexs``) at 1h / 4h / 1d it stores CLOSED candles only (``open_time + interval + grace ≤ now``), prices as the
exact API strings in NUMERIC columns. First contact with a series backfills what the API still serves (latest 5000
candles); later calls fetch from the last stored candle minus ``overlap_bars`` so the overlap can be compared: a stored
candle is immutable, a differing re-fetch raises a ``candle_mismatch`` ops event and is NOT written.

Bounded work per call: series are processed in priority order (markets of any strategy / version first, then the most
overdue, backfills last) until ``max_requests`` or ``max_seconds`` or the request-weight budget runs out; per-series
cursors in ``job_cursors`` (job ``candles``, key ``{coin}|{interval}``: cursor_ms = open time of the newest stored
candle, state.next_due_ms = when the next candle closes) make every call resumable and idempotent.

Read side (API / backtests):
  ``DbCandleSource(db).stored_candles(coin, interval, start_ms, end_ms)`` — closed candles as API-shaped dicts
  (feeds ``app.sandbox.backtest.StoredFirstFetcher``: stored first, API for the rest).
  ``history_days(db, coin, interval)`` / ``listing_history(db, markets, interval)`` — the ≥ 180-day listing rule and
  the "Short history (N days)" (< 365 days) flag.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.errors import AppError
from app.jobs_data import _db
from app.jobs_data.hl import WeightPacer, candle_weight, make_info_client

__all__ = [
    "INTERVALS", "STEP_MS", "HL_MAX_CANDLES", "candles_sync", "SyncReport", "DbCandleSource", "history_days",
    "listing_history", "series_key", "live_perp_markets",
]

INTERVALS = ("1h", "4h", "1d")
STEP_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
HL_MAX_CANDLES = 5000
DAY_MS = 86_400_000
JOB = "candles"
UNIVERSE_JOB, UNIVERSE_KEY = "candles_universe", "all"
_PRICE_FIELDS = ("o", "h", "l", "c")


def series_key(coin: str, interval: str) -> str:
    return f"{coin}|{interval}"


def _valid_coin(c: str) -> bool:
    """Mirror of SQL is_valid_coin(): '^([a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32}$'."""
    import re

    return bool(re.fullmatch(r"([a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32}", c or ""))


@dataclass
class SyncReport:
    markets: int = 0
    series_total: int = 0
    due: int = 0
    processed: int = 0
    backfilled: int = 0
    inserted: int = 0
    mismatches: int = 0
    invalid: int = 0
    gaps: int = 0
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    weight: int = 0
    remaining_due: int = 0
    universe_refreshed: bool = False
    skipped_coins: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["errors"] = self.errors[:20]
        d["skipped_coins"] = self.skipped_coins[:20]
        return d


# --------------------------------------------------------------------------------------------------- universe
def live_perp_markets(info: Any) -> tuple[list[str], list[str]]:
    """(live coins across every perp dex, coins skipped because they fail the DB coin format). 1 + n_dex calls."""
    dexs = info.perp_dexs()
    names = [""] + [n for n in (str((d or {}).get("name") or "") for d in dexs[1:]) if n]
    coins: list[str] = []
    skipped: list[str] = []
    for dex in dict.fromkeys(names):
        try:
            meta = info.meta(dex)
        except AppError:
            if dex == "":
                raise                                       # the validator dex is required
            skipped.append(f"dex:{dex}")                    # one broken builder dex must not stop the others
            continue
        for u in meta.get("universe") or []:
            name = u.get("name")
            if not isinstance(name, str) or u.get("isDelisted"):
                continue
            if dex and not name.startswith(dex + ":"):
                skipped.append(name)
                continue
            (coins if _valid_coin(name) else skipped).append(name)
    return sorted(dict.fromkeys(coins)), skipped


def _universe(db: Any, info: Any, now_ms: int, ttl_ms: int, report: SyncReport,
              pacer: WeightPacer) -> list[str]:
    with _db.transaction(db) as conn:
        _, state = _db.get_cursor(conn, UNIVERSE_JOB, UNIVERSE_KEY)
    cached = state.get("coins")
    if isinstance(cached, list) and cached and 0 <= now_ms - int(state.get("fetched_ms") or 0) < ttl_ms:
        return [str(c) for c in cached]
    for _ in range(int(state.get("dex_count") or 12) + 1):   # perpDexs + one meta per dex
        pacer.spend(20)
    try:
        coins, skipped = live_perp_markets(info)
    except AppError as e:
        if isinstance(cached, list) and cached:            # stale universe beats no sync at all
            report.errors.append(f"universe:{type(e).__name__}")
            return [str(c) for c in cached]
        raise
    report.universe_refreshed = True
    report.skipped_coins = skipped
    with _db.transaction(db) as conn:
        _db.set_cursor(conn, UNIVERSE_JOB, UNIVERSE_KEY, now_ms,
                       {"coins": coins, "fetched_ms": now_ms, "skipped": skipped[:50],
                        "dex_count": len({c.split(":")[0] for c in coins if ":" in c}) + 1}, monotonic=False)
    return coins


def _priority_coins(conn: Any) -> set[str]:
    rs = _db.rows(conn, """
        SELECT DISTINCT m AS coin FROM (
            SELECT unnest(markets) AS m FROM strategies WHERE status IN ('listed', 'review', 'paused')
            UNION ALL
            SELECT unnest(markets) AS m FROM strategy_versions WHERE markets IS NOT NULL
        ) x""")
    return {str(r["coin"]) for r in rs}


# --------------------------------------------------------------------------------------------------- parsing
def _dec(v: Any) -> Optional[Decimal]:
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        return None                                         # floats are never accepted for prices
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _parse_candle(raw: Any, coin: str, interval: str) -> Optional[dict[str, Any]]:
    """Validated API candle → {"t","o","h","l","c","v","n"} (exact strings), or None when malformed."""
    step = STEP_MS[interval]
    if not isinstance(raw, Mapping):
        return None
    t = raw.get("t")
    if isinstance(t, bool) or not isinstance(t, int) or t < 0 or t % step:
        return None
    if raw.get("s") not in (None, coin) or raw.get("i") not in (None, interval):
        return None
    T = raw.get("T")
    if T is not None and T != t + step - 1:
        return None
    vals = {k: _dec(raw.get(k)) for k in (*_PRICE_FIELDS, "v")}
    if any(v is None for v in vals.values()):
        return None
    if any(vals[k] <= 0 for k in _PRICE_FIELDS) or vals["v"] < 0 or vals["h"] < vals["l"]:
        return None
    n = raw.get("n")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        return None
    return {"t": t, **{k: str(raw[k]) for k in (*_PRICE_FIELDS, "v")}, "n": n}


def _same(stored: Mapping[str, Any], fetched: Mapping[str, Any]) -> bool:
    for k in (*_PRICE_FIELDS, "v"):
        a, b = _dec(stored.get(k)), _dec(fetched.get(k))
        if a is None or b is None or a != b:
            return False
    return True


# --------------------------------------------------------------------------------------------------- sync
def candles_sync(db: Any, now: datetime, *, info: Any = None, settings: Any = None,
                 intervals: Sequence[str] = INTERVALS, coins: Optional[Iterable[str]] = None,
                 max_requests: int = 100, max_seconds: float = 240.0, weight_per_minute: int = 600,
                 grace_seconds: int = 60, overlap_bars: int = 2, universe_ttl_seconds: int = 3600,
                 max_consecutive_errors: int = 3, pacer: Optional[WeightPacer] = None) -> dict[str, Any]:
    """One bounded, resumable pass (see module docstring). Returns a JSON-safe summary."""
    for iv in intervals:
        if iv not in STEP_MS:
            raise ValueError(f"unsupported candle interval {iv!r}")
    now_ms = _db.now_ms(now)
    info = make_info_client(settings, info=info)
    pacer = pacer or WeightPacer(weight_per_minute, max_seconds=max_seconds)
    report = SyncReport()
    grace_ms = int(grace_seconds) * 1000

    universe = sorted(set(coins)) if coins is not None else _universe(db, info, now_ms, universe_ttl_seconds * 1000,
                                                                     report, pacer)
    report.markets = len(universe)
    with _db.transaction(db) as conn:
        cursors = _db.all_cursors(conn, JOB)
        priority = _priority_coins(conn)

    due: list[tuple[tuple[int, int, int], str, str, Optional[int], dict[str, Any]]] = []
    for coin in universe:
        for iv in intervals:
            report.series_total += 1
            cur, state = cursors.get(series_key(coin, iv), (None, {}))
            next_due = int(state.get("next_due_ms") or 0)
            if cur is not None and now_ms < next_due:
                continue
            if cur is None and next_due and now_ms < next_due:
                continue                                    # empty market retried after one interval
            backfill = cur is None
            order = (0 if coin in priority else 1, 1 if backfill else 0, next_due)
            due.append((order, coin, iv, cur, state))
    due.sort(key=lambda d: d[0])
    report.due = len(due)

    consecutive_errors = 0
    for idx, (_, coin, iv, cur, state) in enumerate(due):
        step = STEP_MS[iv]
        est = candle_weight(HL_MAX_CANDLES if cur is None else (now_ms - cur) // step + overlap_bars + 1)
        if report.requests >= max_requests or not pacer.can_start(est):
            report.remaining_due = len(due) - idx
            break
        start = max(0, (cur - overlap_bars * step) if cur is not None else now_ms - (HL_MAX_CANDLES + 1) * step)
        pacer.spend(est)
        report.requests += 1
        try:
            raw = info.candle_snapshot(coin, iv, start, now_ms)
        except AppError as e:
            consecutive_errors += 1
            report.errors.append(f"{coin}|{iv}:{type(e).__name__}")
            if consecutive_errors >= max_consecutive_errors:
                report.remaining_due = len(due) - idx - 1
                break
            continue
        consecutive_errors = 0
        pacer.settle(candle_weight(len(raw)), est)
        try:
            _store_series(db, coin, iv, raw, cur, state, now=now, now_ms=now_ms, grace_ms=grace_ms, report=report)
        except Exception as e:  # noqa: BLE001 - one bad series must not stop the others; retried next call
            report.errors.append(f"{coin}|{iv}:store:{type(e).__name__}")
            _db.log.error("candles_store_failed", exc_info=True, extra={"fields": {"coin": coin, "interval": iv}})
            continue
        report.processed += 1
        if cur is None:
            report.backfilled += 1
    report.weight = pacer.spent
    _db.log.info("candles_sync_done", extra={"fields": report.as_dict()})
    return report.as_dict()


def _store_series(db: Any, coin: str, interval: str, raw: Any, cur: Optional[int], state: Mapping[str, Any], *,
                  now: datetime, now_ms: int, grace_ms: int, report: SyncReport) -> None:
    step = STEP_MS[interval]
    closed: dict[int, dict[str, Any]] = {}
    invalid = 0
    for item in raw if isinstance(raw, list) else []:
        c = _parse_candle(item, coin, interval)
        if c is None:
            invalid += 1
            continue
        if c["t"] + step + grace_ms <= now_ms:
            closed[c["t"]] = c
    key = series_key(coin, interval)
    with _db.transaction(db) as conn:
        if invalid:
            report.invalid += invalid
            _db.ops_alert(conn, "candle_invalid", {"coin": coin, "interval": interval, "count": invalid},
                          severity="warn", dedup_key=f"candle_invalid:{key}:{now_ms // DAY_MS}")
        stored: dict[int, dict[str, Any]] = {}
        if closed:
            lo, hi = min(closed), max(closed)
            for r in _db.rows(conn, f"""
                    SELECT {_db.ts_to_ms('open_time')} AS t, o::text AS o, h::text AS h, l::text AS l, c::text AS c,
                           v::text AS v
                      FROM candles
                     WHERE coin = :coin AND interval = :iv
                       AND open_time BETWEEN {_db.ms_to_ts('CAST(:lo AS bigint)')} AND {_db.ms_to_ts('CAST(:hi AS bigint)')}""",
                              coin=coin, iv=interval, lo=lo, hi=hi):
                stored[int(r["t"])] = r
        new_rows = []
        for t in sorted(closed):
            s = stored.get(t)
            if s is None:
                new_rows.append(closed[t])
            elif not _same(s, closed[t]):
                report.mismatches += 1
                _db.ops_alert(conn, "candle_mismatch", {
                    "coin": coin, "interval": interval, "open_time_ms": t,
                    "stored": {k: s[k] for k in (*_PRICE_FIELDS, "v")},
                    "fetched": {k: closed[t][k] for k in (*_PRICE_FIELDS, "v")}},
                    severity="warn", dedup_key=f"candle_mismatch:{key}:{t}")
        if cur is not None and closed and min(closed) > cur + step:
            report.gaps += 1                                 # downtime longer than the API's window
            _db.ops_alert(conn, "candle_gap", {"coin": coin, "interval": interval, "after_ms": cur,
                                               "resumed_ms": min(closed)},
                          severity="info", dedup_key=f"candle_gap:{key}:{cur}")
        if new_rows:
            ins = _db.rows(conn, f"""
                INSERT INTO candles (coin, interval, open_time, o, h, l, c, v, trades, source, fetched_at)
                SELECT :coin, :iv, {_db.ms_to_ts('r.t')}, CAST(r.o AS numeric), CAST(r.h AS numeric),
                       CAST(r.l AS numeric), CAST(r.c AS numeric), CAST(r.v AS numeric), r.n, 'hyperliquid',
                       CAST(:now AS timestamptz)
                  FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(t bigint, o text, h text, l text, c text,
                                                                      v text, n integer)
                ON CONFLICT (coin, interval, open_time) DO NOTHING
                RETURNING {_db.ts_to_ms('open_time')} AS t""",
                               coin=coin, iv=interval, now=now, rows=_db.jdump(new_rows))
            report.inserted += len(ins)
        newest = max([*closed, *( [cur] if cur is not None else [])], default=None)
        if newest is None:
            _db.set_cursor(conn, JOB, key, None, {"next_due_ms": now_ms + step, "empty": True}, monotonic=True)
        else:
            _db.set_cursor(conn, JOB, key, newest, {"next_due_ms": newest + 2 * step + grace_ms}, monotonic=True)


# --------------------------------------------------------------------------------------------------- read side
class DbCandleSource:
    """Stored closed candles for backtests (``app.sandbox.backtest.StoredFirstFetcher``). ``db`` = DatabasePort or
    a connection / SqlRunner. Returns API-shaped dicts with exact decimal strings, oldest first."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def stored_candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        if interval not in STEP_MS:
            return []
        with _db.transaction(self.db) as conn:
            rs = _db.rows(conn, f"""
                SELECT {_db.ts_to_ms('open_time')} AS t, o::text AS o, h::text AS h, l::text AS l, c::text AS c,
                       v::text AS v, trades AS n
                  FROM candles
                 WHERE coin = :coin AND interval = :iv
                   AND open_time BETWEEN {_db.ms_to_ts('CAST(:s AS bigint)')} AND {_db.ms_to_ts('CAST(:e AS bigint)')}
                 ORDER BY open_time""", coin=coin, iv=interval, s=max(0, int(start_ms)), e=max(0, int(end_ms)))
        step = STEP_MS[interval]
        return [{"t": int(r["t"]), "T": int(r["t"]) + step - 1, "s": coin, "i": interval, "o": r["o"], "h": r["h"],
                 "l": r["l"], "c": r["c"], "v": r["v"], "n": r["n"]} for r in rs]


    def is_backfilled(self, coin: str, interval: str) -> bool:
        """True once candles_sync has fetched this series (its first fetch backfills all the API serves)."""
        with _db.transaction(self.db) as conn:
            return _db.one(conn, "SELECT 1 AS x FROM job_cursors WHERE job = :j AND key = :k AND cursor_ms IS NOT NULL",
                           j=JOB, k=series_key(coin, interval)) is not None


def _first_traded(rows: Iterable[Mapping[str, Any]]) -> Optional[int]:
    for r in rows:
        n = r.get("n")
        traded = (n > 0) if isinstance(n, int) else ((_dec(r.get("v")) or Decimal(0)) > 0)
        if traded:
            return int(r["t"])
    return None


def history_days(db: Any, coin: str, interval: str = "1d", *, api: Any = None, now_ms: Optional[int] = None) -> int:
    """Days of backtestable history for (coin, interval): from the first TRADED candle (leading zero-volume
    pre-listing bars excluded, like the backtest) to the end of the newest closed candle, over stored candles and —
    when ``api`` (a ``CandleFetcher``: ``candles(coin, interval, start_ms, end_ms)``) is given — what the API
    serves. Whole days, floored."""
    step = STEP_MS[interval]
    with _db.transaction(db) as conn:
        r = _db.one(conn, f"""
            SELECT {_db.ts_to_ms('min(open_time) FILTER (WHERE coalesce(trades > 0, v > 0))')} AS first_t,
                   {_db.ts_to_ms('max(open_time)')} AS last_t
              FROM candles WHERE coin = :coin AND interval = :iv""", coin=coin, iv=interval)
    first = int(r["first_t"]) if r and r.get("first_t") is not None else None
    last = int(r["last_t"]) if r and r.get("last_t") is not None else None
    if api is not None:
        import time as _time

        now = now_ms if now_ms is not None else int(_time.time() * 1000)
        closed = [c for c in api.candles(coin, interval, 0, now) if int(c["t"]) + step <= now]
        a_first = _first_traded(closed)
        if a_first is not None:
            first = a_first if first is None else min(first, a_first)
            a_last = int(closed[-1]["t"])
            last = a_last if last is None else max(last, a_last)
    if first is None or last is None or last < first:
        return 0
    return (last + step - first) // DAY_MS


def listing_history(db: Any, markets: Sequence[str], interval: str = "1d", *, api: Any = None,
                    risk: Any = None, now_ms: Optional[int] = None) -> dict[str, Any]:
    """SPEC §12 listing history rule for a version's markets: ``eligible`` when every market has ≥
    ``risk.min_listing_history_days`` (180) days; ``short_history`` (show "Short history (N days)") when the
    shortest is < ``risk.short_history_warning_days`` (365). ``days`` = the shortest market's history."""
    if risk is None:
        from app.config import RiskLimits

        risk = RiskLimits()
    per = {m: history_days(db, m, interval, api=api, now_ms=now_ms) for m in markets}
    days = min(per.values()) if per else 0
    return {"interval": interval, "days_by_market": per, "days": days,
            "eligible": bool(per) and days >= risk.min_listing_history_days,
            "short_history": days < risk.short_history_warning_days,
            "min_days": risk.min_listing_history_days, "warn_below_days": risk.short_history_warning_days,
            "label": f"Short history ({days} days)" if days < risk.short_history_warning_days else None}
