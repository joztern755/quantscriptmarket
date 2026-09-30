"""``funding_scan(db, now)`` — ``/internal/funding-scan`` (SPEC §1.1 "plus funding payments on the strategy's coins
while the subscription held a position").

Per tracked trading address (same scope as fills_ingest): ``userFunding`` since the address cursor (minus an overlap)
→ every funding payment of the account is stored in ``funding_events`` (UNIQUE(trading_address, coin, time); the hash
is all zeros so it cannot be the key) with the account amount in ``usdc_micro`` and, when a subscription held the coin,
``subscription_id`` + ``attributed_micro`` from ``app.hl.fills.attribute_funding`` (conservative policy: floored toward
−∞; estimated daily-aggregate INCOME dropped, estimated COSTS kept — attributed PnL is never overstated).

Daily-bucket behaviour (VERIFIED, app.hl.fills): payments younger than ~8 days come back HOURLY; older ones only as
DAILY aggregates (time = UTC midnight, ``nSamples`` merged payments). Run this job at least daily (hourly recommended)
so it only ever sees hourly entries. When an outage or a first scan reaches further back:
  * requests start at the UTC midnight of the cursor day, so the aggregate bucket of that day is returned;
  * an aggregate whose day already has stored HOURLY rows is reduced to the RESIDUAL (usdc − Σ stored hourly,
    nSamples − count); the residual is attributed cost-only (never income) and raises ``funding_partial_bucket``;
  * an aggregate colliding with a stored row at the exact same (coin, time) cannot be stored → ops event.
Anomalies from attribution (account flat/opposite to the subscription, mixed buckets) → ``funding_anomaly``.
Settlement's ``pnl_since`` sums ``funding_events.attributed_micro`` (never ``usdc_micro``) for the subscription.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Mapping, Optional, Sequence

from app.errors import AppError, ValidationFailed
from app.hl.fills import (
    AttributedFunding,
    FundingEvent,
    attribute_funding,
    parse_fill,
    parse_funding,
    position_before,
    position_timeline,
)
from app.jobs_data import _db
from app.jobs_data.fills import _in_scope, tracked_subscriptions
from app.jobs_data.hl import WeightPacer, list_weight, make_info_client
from app.money import MICRO, to_micro

__all__ = ["funding_scan", "FUNDING_PAGE"]

JOB = "funding"
FUNDING_PAGE = 500          # page size of the funding endpoints (fundingHistory: 500; userFunding assumed the same)
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
AGGREGATE_AGE_MS = 7 * DAY_MS   # older than this may come back aggregated (observed ~8 days) → start at midnight
_COIN_RE = re.compile(r"([a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32}")


@dataclass
class FundingReport:
    addresses: int = 0
    processed: int = 0
    fetched: int = 0
    inserted: int = 0
    attributed: int = 0
    attributed_micro: int = 0
    estimated: int = 0
    partial_buckets: int = 0
    anomalies: int = 0
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    weight: int = 0
    remaining: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["errors"] = self.errors[:20]
        return d


def funding_scan(db: Any, now: datetime, *, info: Any = None, settings: Any = None, max_addresses: int = 200,
                 max_pages_per_address: int = 10, max_seconds: float = 240.0, weight_per_minute: int = 600,
                 overlap_minutes: int = 90, recent_cancel_days: int = 7, aggregate_income: str = "drop",
                 pacer: Optional[WeightPacer] = None) -> dict[str, Any]:
    if aggregate_income not in ("drop", "estimate"):
        raise ValidationFailed("aggregate_income must be 'drop' or 'estimate'")
    now_ms = _db.now_ms(now)
    info = make_info_client(settings, info=info)
    pacer = pacer or WeightPacer(weight_per_minute, max_seconds=max_seconds)
    report = FundingReport()
    overlap_ms = overlap_minutes * 60_000
    with _db.transaction(db) as conn:
        by_addr = tracked_subscriptions(conn, now_ms, recent_cancel_days)
        cursors = _db.all_cursors(conn, JOB)
    addrs = sorted(by_addr, key=lambda a: (cursors.get(a, (None, {}))[0] or -1, a))
    report.addresses = len(addrs)
    for i, addr in enumerate(addrs):
        if report.processed >= max_addresses or not pacer.can_start(list_weight(FUNDING_PAGE)):
            report.remaining = len(addrs) - i
            break
        subs = by_addr[addr]
        if not any(_in_scope(s, now_ms, recent_cancel_days) for s in subs):
            continue
        cur, _state = cursors.get(addr, (None, {}))
        start = (cur - overlap_ms) if cur is not None else min(int(s["created_ms"]) for s in subs)
        if cur is None or now_ms - start > AGGREGATE_AGE_MS:
            start -= start % DAY_MS                         # include the aggregate bucket of the start day
        try:
            raw, complete = _fetch(info, pacer, report, addr, max(0, start), now_ms, max_pages_per_address)
        except AppError as e:
            report.errors.append(f"{_db.short_addr(addr)}:{type(e).__name__}")
            continue
        try:
            _store_address(db, addr, subs, raw, complete, cur, start_ms=max(0, start), now_ms=now_ms,
                           overlap_ms=overlap_ms, aggregate_income=aggregate_income, report=report)
        except Exception as e:  # noqa: BLE001 - isolate addresses; cursor unchanged → retried next call
            report.errors.append(f"{_db.short_addr(addr)}:store:{type(e).__name__}")
            _db.log.error("funding_store_failed", exc_info=True, extra={"fields": {"address": _db.short_addr(addr)}})
            continue
        report.processed += 1
    report.weight = pacer.spent
    _db.log.info("funding_scan_done", extra={"fields": report.as_dict()})
    return report.as_dict()


def _fetch(info: Any, pacer: WeightPacer, report: FundingReport, addr: str, start_ms: int, end_ms: int,
           max_pages: int) -> tuple[list[dict], bool]:
    out: dict[tuple[int, str], dict] = {}
    cursor = start_ms
    for _ in range(max_pages):
        est = list_weight(FUNDING_PAGE)
        pacer.spend(est)
        report.requests += 1
        page = info.user_funding(addr, cursor, end_ms)
        pacer.settle(list_weight(len(page)), est)
        new = 0
        last = cursor
        for e in page:
            t = e.get("time")
            coin = (e.get("delta") or {}).get("coin")
            if not isinstance(t, int) or not isinstance(coin, str):
                continue
            last = max(last, t)
            if (t, coin) not in out:
                out[(t, coin)] = e
                new += 1
        if len(page) < FUNDING_PAGE or new == 0:
            return [out[k] for k in sorted(out)], True
        cursor = last
    return [out[k] for k in sorted(out)], False


def _sub_fills(conn: Any, sub_id: str, coins: Sequence[str], from_ms: int) -> tuple[list, dict[str, Decimal]]:
    """The subscription's own fills from ``from_ms`` (parsed) and its signed position per coin before that."""
    starts: dict[str, Decimal] = {}
    for r in _db.rows(conn, f"""
            SELECT coin, sum(CASE WHEN side = 'buy' THEN sz ELSE -sz END)::text AS pos
              FROM fills WHERE subscription_id = CAST(:s AS uuid)
               AND time < {_db.ms_to_ts('CAST(:t AS bigint)')}
             GROUP BY coin""", s=sub_id, t=from_ms):
        starts[str(r["coin"])] = Decimal(str(r["pos"]))
    fills = []
    for r in _db.rows(conn, f"""
            SELECT raw FROM fills WHERE subscription_id = CAST(:s AS uuid)
               AND time >= {_db.ms_to_ts('CAST(:t AS bigint)')} AND raw IS NOT NULL
             ORDER BY time""", s=sub_id, t=from_ms):
        raw = _db.jload(r["raw"])
        try:
            f = parse_fill(raw)
        except ValidationFailed:
            continue
        if f.coin in coins:
            fills.append(f)
    return fills, starts


def _usdc_micro(d: Decimal) -> int:
    return to_micro(d, ROUND_FLOOR)


def _store_address(db: Any, addr: str, subs: Sequence[Mapping[str, Any]], raw: list[dict], complete: bool,
                   cur: Optional[int], *, start_ms: int, now_ms: int, overlap_ms: int, aggregate_income: str,
                   report: FundingReport) -> None:
    report.fetched += len(raw)
    events: list[tuple[FundingEvent, dict]] = []
    for r in raw:
        if (r.get("delta") or {}).get("type") != "funding":
            continue
        try:
            ev = parse_funding(r)
        except ValidationFailed:
            continue
        if _COIN_RE.fullmatch(ev.coin):
            events.append((ev, r))
    max_time = max((ev.time_ms for ev, _ in events), default=None)
    with _db.transaction(db) as conn:
        stored: dict[tuple[str, int], dict[str, Any]] = {}
        if events:
            for r in _db.rows(conn, f"""
                    SELECT coin, {_db.ts_to_ms('time')} AS t, usdc_micro, n_samples
                      FROM funding_events WHERE trading_address = :a
                       AND time >= {_db.ms_to_ts('CAST(:s AS bigint)')}""",
                              a=addr, s=min(ev.time_ms for ev, _ in events) - DAY_MS):
                stored[(str(r["coin"]), int(r["t"]))] = r
        new: list[tuple[FundingEvent, dict, bool]] = []      # (event, raw, residual?)
        for ev, r in events:
            key = (ev.coin, ev.time_ms)
            if key in stored:
                if ev.is_aggregate and stored[key].get("n_samples") is None:
                    _db.ops_alert(conn, "funding_bucket_collision", {
                        "coin": ev.coin, "time_ms": ev.time_ms, "address": _db.short_addr(addr)},
                        severity="warn", dedup_key=f"funding_bucket_collision:{addr}:{ev.coin}:{ev.time_ms}")
                continue
            if ev.is_aggregate:
                hourly = [s for (c, t), s in stored.items()
                          if c == ev.coin and ev.time_ms <= t < ev.time_ms + DAY_MS and s.get("n_samples") is None]
                if hourly:
                    n_res = int(ev.n_samples or 0) - len(hourly)
                    if n_res <= 0:
                        continue                              # the day is fully covered by hourly rows
                    residual = ev.usdc - sum((Decimal(int(s["usdc_micro"])) / MICRO for s in hourly), Decimal(0))
                    ev = FundingEvent(time_ms=ev.time_ms, coin=ev.coin, usdc=residual, szi=ev.szi,
                                      funding_rate=ev.funding_rate, hash=ev.hash, n_samples=n_res)
                    r = {**r, "residual_of": {"usdc": r["delta"].get("usdc"), "nSamples": r["delta"].get("nSamples"),
                                              "hourly_rows": len(hourly)}}
                    report.partial_buckets += 1
                    _db.ops_alert(conn, "funding_partial_bucket", {
                        "coin": ev.coin, "time_ms": ev.time_ms, "residual_samples": n_res,
                        "address": _db.short_addr(addr)},
                        severity="warn", dedup_key=f"funding_partial_bucket:{addr}:{ev.coin}:{ev.time_ms}")
                    new.append((ev, r, True))
                    continue
            new.append((ev, r, False))

        attributed: dict[tuple[str, int], AttributedFunding] = {}
        if new:
            earliest = min(ev.time_ms for ev, _, _ in new)
            for s in subs:
                coins = [c for c in s["markets"]]
                sub_start = int(s["created_ms"])
                sub_end = int(s["ended_ms"]) if s.get("ended_ms") is not None else None
                mine = [(ev, r, res) for ev, r, res in new if ev.coin in coins
                        and ev.time_ms + (DAY_MS if ev.is_aggregate else 0) > sub_start
                        and (sub_end is None or ev.time_ms < sub_end)]
                if not mine:
                    continue
                window_from = min(earliest, sub_start) - DAY_MS
                fills, starts = _sub_fills(conn, s["id"], coins, window_from)
                normal = [r for ev, r, res in mine if not res]
                fa = attribute_funding(normal, subscription_id=s["id"], fills=fills, coins=coins,
                                       start_ms=sub_start - sub_start % DAY_MS, end_ms=sub_end,
                                       start_positions=starts, aggregate_income=aggregate_income)
                for a in fa.attributed:
                    attributed.setdefault((a.coin, a.time_ms), a)
                for ev, reason in fa.anomalies:
                    report.anomalies += 1
                    _db.ops_alert(conn, "funding_anomaly", {
                        "subscription_id": s["id"], "coin": ev.coin, "time_ms": ev.time_ms, "reason": reason[:120],
                        "address": _db.short_addr(addr)},
                        severity="warn", dedup_key=f"funding_anomaly:{addr}:{ev.coin}:{ev.time_ms}")
                # residual buckets: cost-only (never income), only if the subscription held the coin that day
                for ev, _r, res in mine:
                    if not res or (ev.coin, ev.time_ms) in attributed:
                        continue
                    tl = position_timeline(fills, ev.coin, starts.get(ev.coin, Decimal(0)))
                    held = any(position_before(tl, ev.time_ms + h * HOUR_MS, starts.get(ev.coin, Decimal(0))) != 0
                               for h in range(24))
                    if held:
                        attributed[(ev.coin, ev.time_ms)] = AttributedFunding(
                            s["id"], ev.coin, ev.time_ms, _usdc_micro(min(Decimal(0), ev.usdc)), Decimal(1), ev,
                            estimated=True)

        rows = []
        for ev, r, _res in new:
            a = attributed.get((ev.coin, ev.time_ms))
            rows.append({"coin": ev.coin, "t": ev.time_ms, "usdc": _usdc_micro(ev.usdc),
                         "sub": a.subscription_id if a else None, "att": a.usdc_micro if a else None,
                         "n": ev.n_samples, "est": bool(a.estimated) if a else ev.is_aggregate,
                         "szi": format(ev.szi, "f"), "rate": format(ev.funding_rate, "f"), "raw": r})
        if rows:
            ins = _db.rows(conn, f"""
                INSERT INTO funding_events (subscription_id, trading_address, coin, time, usdc_micro, raw,
                                            attributed_micro, n_samples, estimated, szi, funding_rate)
                SELECT CAST(r.sub AS uuid), :a, r.coin, {_db.ms_to_ts('r.t')}, r.usdc, r.raw, r.att, r.n, r.est,
                       CAST(r.szi AS numeric), CAST(r.rate AS numeric)
                  FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(sub text, coin text, t bigint, usdc bigint,
                                                                      raw jsonb, att bigint, n integer, est boolean,
                                                                      szi text, rate text)
                ON CONFLICT (trading_address, coin, time) DO NOTHING
                RETURNING coin, {_db.ts_to_ms('time')} AS t, attributed_micro, estimated""",
                              a=addr, rows=_db.jdump(rows))
            report.inserted += len(ins)
            for r in ins:
                if r.get("attributed_micro") is not None:
                    report.attributed += 1
                    report.attributed_micro += int(r["attributed_micro"])
                    report.estimated += 1 if r.get("estimated") else 0
        new_cur = (max(max_time or 0, now_ms - overlap_ms) if complete
                   else (max_time if max_time is not None else cur))
        _db.set_cursor(conn, JOB, addr, new_cur, {"last_run_ms": now_ms, "complete": complete,
                                                  "start_ms": start_ms})
