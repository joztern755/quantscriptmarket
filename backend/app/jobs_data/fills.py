"""``fills_ingest(db, now)`` — ``/internal/fills-ingest`` (SPEC §1.1, §12 trade alerts).

For every trading address with a live (pending|active|past_due|reduce_only|paused_user|closing) or recently cancelled
subscription: ``userFillsByTime`` since the address cursor (minus an overlap; inserts are idempotent on
UNIQUE(trading_address, tid)) → ``app.hl.fills.attribute_fills`` (orders.cloid → subscription; subscription windows as
the crash fallback) → ``fills`` rows with ``net_pnl_micro = floor((closedPnl − fee) × 1e6)`` → trade events in
``events_outbox``.

Which fills are stored: only fills carrying OUR cloid prefix (attributed, or ``subscription_id`` NULL + ops event when no
subscription matches). The user's own trades are never stored: their ``builderFee`` may belong to another builder and
settlement recognises every stored fill's builder fee as ours.

PnL contract with settlement (``app.execution.settlement``): this job does NOT touch ``subscriptions.cum_pnl_micro`` /
``pnl_cursor``. ``Settlement._settle_profit_share`` reads ``SettlementRepo.pnl_since(sub, pnl_cursor, cutoff)`` and
itself stores cum_pnl / hwm / cursor via ``save_profit_share``; updating them here would double count. ``pnl_since``
must return realized = Σ ``fills.net_pnl_micro`` and funding = Σ ``funding_events.attributed_micro`` of the subscription
with ``pnl_cursor < time <= cutoff``. A fill ingested AFTER its day was settled (time ≤ pnl_cursor) can no longer be
counted: it raises a critical ``fill_after_settlement`` ops event — schedule this job before the 00:30 settlement.

Trade events (one per order = cloid, from its newly stored fills): ``trade_opened`` (position was flat),
``trade_closed`` (position ends flat), ``trade_resized`` (anything else; ``flipped`` when the sign changed). Payload:
subscription_id, strategy_id, coin, side, size, avg_px, notional_micro, fees_micro, builder_fee_micro,
realized_pnl_micro (Σ closedPnl, gross), net_pnl_micro (Σ closedPnl − fee), position_before, position_after, time_ms,
fills. Positions are the ACCOUNT's (``startPosition``), which equals the subscription's while it is the only trader.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.errors import AppError
from app.hl.client import is_platform_cloid
from app.hl.fills import AttributedFill, Fill, SubscriptionWindow, attribute_fills
from app.jobs_data import _db
from app.jobs_data.hl import WeightPacer, list_weight, make_info_client, make_pacer
from app.money import to_micro

__all__ = ["fills_ingest", "trade_events", "tracked_subscriptions", "LIVE_STATUSES", "FILLS_PAGE"]

JOB = "fills"
FILLS_PAGE = 2000
LIVE_STATUSES = ("pending", "active", "past_due", "reduce_only", "paused_user", "closing")
_COIN_RE = re.compile(r"([a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32}")
ZERO = Decimal(0)


@dataclass
class FillsReport:
    addresses: int = 0
    processed: int = 0
    fetched: int = 0
    inserted: int = 0
    attributed: int = 0
    unattributed: int = 0
    foreign: int = 0
    rejected: int = 0
    events: int = 0
    late_fills: int = 0
    incomplete: int = 0
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    weight: int = 0
    remaining: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["errors"] = self.errors[:20]
        return d


# ------------------------------------------------------------------------------------------------ scope
def tracked_subscriptions(conn: Any, now_ms: int, recent_cancel_days: int) -> dict[str, list[dict[str, Any]]]:
    """{trading_address: [subscription rows]} for every address that has a live or recently cancelled subscription
    (all of that address's subscriptions are returned, for window attribution)."""
    rs = _db.rows(conn, f"""
        WITH scope AS (
            SELECT DISTINCT trading_address FROM subscriptions
             WHERE status::text IN ('pending', 'active', 'past_due', 'reduce_only', 'paused_user', 'closing')
                OR coalesce(cancelled_at, status_changed_at) > {_db.ms_to_ts('CAST(:since AS bigint)')})
        SELECT s.id::text AS id, s.user_id::text AS user_id, s.strategy_id::text AS strategy_id,
               s.trading_address AS trading_address, s.status::text AS status,
               {_db.ts_to_ms('s.created_at')} AS created_ms,
               {_db.ts_to_ms("coalesce(s.cancelled_at, CASE WHEN s.status = 'cancelled' THEN s.status_changed_at END)")}
                   AS ended_ms,
               {_db.ts_to_ms('s.pnl_cursor')} AS pnl_cursor_ms,
               coalesce(v.markets, st.markets) AS markets
          FROM subscriptions s
          JOIN scope ON scope.trading_address = s.trading_address
          JOIN strategies st ON st.id = s.strategy_id
          JOIN strategy_versions v ON v.id = s.strategy_version_id
         ORDER BY s.trading_address, s.created_at""", since=now_ms - recent_cancel_days * 86_400_000)
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rs:
        r["markets"] = [str(m) for m in (r.get("markets") or [])]
        out.setdefault(str(r["trading_address"]), []).append(r)
    return out


def _windows(addr: str, subs: Sequence[Mapping[str, Any]]) -> list[SubscriptionWindow]:
    return [SubscriptionWindow(subscription_id=s["id"], trading_address=addr, coins=frozenset(s["markets"]),
                               start_ms=int(s["created_ms"]),
                               end_ms=int(s["ended_ms"]) if s.get("ended_ms") is not None else None)
            for s in subs]


def _in_scope(s: Mapping[str, Any], now_ms: int, recent_cancel_days: int) -> bool:
    if s["status"] != "cancelled":
        return True
    end = s.get("ended_ms")
    return end is not None and int(end) > now_ms - recent_cancel_days * 86_400_000


# ------------------------------------------------------------------------------------------------ events
def _fmt(d: Decimal) -> str:
    if d == d.to_integral_value():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")


def trade_events(attributed: Iterable[AttributedFill]) -> list[tuple[str, str, dict[str, Any]]]:
    """Group a subscription's new fills per order (cloid) → [(subscription_id, kind, payload)]. Pure."""
    groups: dict[tuple[str, str], list[Fill]] = {}
    for a in attributed:
        groups.setdefault((a.subscription_id, a.fill.cloid or f"oid:{a.fill.oid}"), []).append(a.fill)
    out: list[tuple[str, str, dict[str, Any]]] = []
    for (sub_id, order_key), fills in sorted(groups.items(), key=lambda kv: min(f.time_ms for f in kv[1])):
        side = fills[0].side
        size = sum((f.sz for f in fills), ZERO)
        if size <= 0:
            continue
        signed = size if side == "B" else -size
        start = min(f.start_position for f in fills) if side == "B" else max(f.start_position for f in fills)
        end = start + signed
        notional = sum((f.px * f.sz for f in fills), ZERO)
        avg_px = (notional / size).quantize(Decimal("1e-8"), rounding=ROUND_FLOOR).normalize()
        closed_pnl = sum((f.closed_pnl for f in fills), ZERO)
        fee = sum((f.fee for f in fills), ZERO)
        builder = sum((f.builder_fee for f in fills), ZERO)
        if start == 0:
            kind = "trade_opened"
        elif end == 0:
            kind = "trade_closed"
        else:
            kind = "trade_resized"
        payload = {
            "subscription_id": sub_id, "coin": fills[0].coin, "side": "buy" if side == "B" else "sell",
            "size": _fmt(size), "avg_px": _fmt(avg_px), "notional_micro": to_micro(notional, ROUND_FLOOR),
            "fees_micro": to_micro(fee, ROUND_FLOOR), "builder_fee_micro": to_micro(builder, ROUND_FLOOR),
            "realized_pnl_micro": to_micro(closed_pnl, ROUND_FLOOR),
            "net_pnl_micro": to_micro(closed_pnl - fee, ROUND_FLOOR),
            "position_before": _fmt(start), "position_after": _fmt(end),
            "flipped": start != 0 and end != 0 and (start > 0) != (end > 0),
            "time_ms": max(f.time_ms for f in fills), "fills": len(fills), "order": order_key,
            "liquidation": any(f.is_liquidation for f in fills),
        }
        out.append((sub_id, kind, payload))
    return out


def _fill_row(f: Fill, sub_id: Optional[str], via: Optional[str]) -> dict[str, Any]:
    return {"sub": sub_id, "coin": f.coin, "tid": f.tid, "oid": f.oid, "px": format(f.px, "f"), "sz": format(f.sz, "f"),
            "side": "buy" if f.side == "B" else "sell", "cpm": f.closed_pnl_micro, "fm": f.fee_micro,
            "bfm": f.builder_fee_micro, "npm": f.net_pnl_micro, "cloid": f.cloid, "t": f.time_ms,
            "raw": dict(f.raw), "via": via}


_INSERT_FILLS = f"""
    INSERT INTO fills (subscription_id, trading_address, coin, tid, oid, px, sz, side, closed_pnl_micro, fee_micro,
                       builder_fee_micro, net_pnl_micro, cloid, time, raw, attributed_via)
    SELECT CAST(r.sub AS uuid), :addr, r.coin, r.tid, r.oid, CAST(r.px AS numeric), CAST(r.sz AS numeric),
           CAST(r.side AS order_side), r.cpm, r.fm, r.bfm, r.npm, r.cloid, {_db.ms_to_ts('r.t')}, r.raw, r.via
      FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(sub text, coin text, tid bigint, oid bigint, px text, sz text,
                                                          side text, cpm bigint, fm bigint, bfm bigint, npm bigint,
                                                          cloid text, t bigint, raw jsonb, via text)
    ON CONFLICT (trading_address, tid) DO NOTHING
    RETURNING tid"""


# ------------------------------------------------------------------------------------------------ job
def fills_ingest(db: Any, now: datetime, *, info: Any = None, settings: Any = None, max_addresses: int = 200,
                 max_pages_per_address: int = 5, max_seconds: float = 240.0, weight_per_minute: int = 600,
                 overlap_minutes: int = 10, recent_cancel_days: int = 7,
                 pacer: Optional[WeightPacer] = None, rate_budget: Any = None) -> dict[str, Any]:
    """One bounded, resumable pass over the tracked trading addresses (see module docstring)."""
    now_ms = _db.now_ms(now)
    pacer = pacer or make_pacer(db, settings, info=info, weight_per_minute=weight_per_minute,
                                max_seconds=max_seconds, rate_budget=rate_budget)
    info = make_info_client(settings, info=info)
    report = FillsReport()
    overlap_ms = overlap_minutes * 60_000
    with _db.transaction(db) as conn:
        by_addr = tracked_subscriptions(conn, now_ms, recent_cancel_days)
        cursors = _db.all_cursors(conn, JOB)
    # least recently scanned first (fairness when the per-call bound is hit)
    addrs = sorted(by_addr, key=lambda a: (cursors.get(a, (None, {}))[0] or -1, a))
    report.addresses = len(addrs)
    for i, addr in enumerate(addrs):
        if report.processed >= max_addresses or not pacer.can_start(list_weight(FILLS_PAGE)):
            report.remaining = len(addrs) - i
            break
        subs = by_addr[addr]
        cur, state = cursors.get(addr, (None, {}))
        scoped = [s for s in subs if _in_scope(s, now_ms, recent_cancel_days)]
        if not scoped:
            continue
        start = (cur - overlap_ms) if cur is not None else min(int(s["created_ms"]) for s in scoped) - 3_600_000
        try:
            raw, complete = _fetch(info, pacer, report, addr, max(0, start), now_ms, max_pages_per_address)
        except AppError as e:
            report.errors.append(f"{_db.short_addr(addr)}:{type(e).__name__}")
            continue
        try:
            _store_address(db, addr, subs, raw, complete, cur, now_ms=now_ms, overlap_ms=overlap_ms, report=report)
        except Exception as e:  # noqa: BLE001 - isolate addresses; the cursor did not move, so it is retried
            report.errors.append(f"{_db.short_addr(addr)}:store:{type(e).__name__}")
            _db.log.error("fills_store_failed", exc_info=True, extra={"fields": {"address": _db.short_addr(addr)}})
            continue
        report.processed += 1
    report.weight = pacer.spent
    _db.log.info("fills_ingest_done", extra={"fields": report.as_dict()})
    return report.as_dict()


def _fetch(info: Any, pacer: WeightPacer, report: FillsReport, addr: str, start_ms: int, end_ms: int,
           max_pages: int) -> tuple[list[dict], bool]:
    """Time-cursor pagination (oldest first, ≤ 2000 per page, restart AT the last time seen; dedupe by tid)."""
    out: dict[int, dict] = {}
    cursor = start_ms
    for _ in range(max_pages):
        est = list_weight(FILLS_PAGE)
        pacer.spend(est)
        report.requests += 1
        page = info.user_fills_by_time(addr, cursor, end_ms)
        pacer.settle(list_weight(len(page)), est)
        new = 0
        last = cursor
        for f in page:
            t, tid = f.get("time"), f.get("tid")
            if isinstance(t, int):
                last = max(last, t)
            if isinstance(tid, int) and tid not in out:
                out[tid] = f
                new += 1
        if len(page) < FILLS_PAGE or new == 0:
            return list(out.values()), True
        cursor = last
    return list(out.values()), False


def _store_address(db: Any, addr: str, subs: Sequence[Mapping[str, Any]], raw: list[dict], complete: bool,
                   cur: Optional[int], *, now_ms: int, overlap_ms: int, report: FillsReport) -> None:
    report.fetched += len(raw)
    by_id = {s["id"]: s for s in subs}
    cloids = sorted({str(f.get("cloid")).lower() for f in raw if is_platform_cloid(f.get("cloid"))})
    max_time = max((int(f["time"]) for f in raw if isinstance(f.get("time"), int)), default=None)
    with _db.transaction(db) as conn:
        cmap: dict[str, str] = {}
        if cloids:
            for r in _db.rows(conn, """
                    SELECT o.cloid, o.subscription_id::text AS subscription_id
                      FROM orders o JOIN subscriptions s ON s.id = o.subscription_id
                     WHERE s.trading_address = :addr
                       AND o.cloid IN (SELECT jsonb_array_elements_text(CAST(:cl AS jsonb)))""",
                              addr=addr, cl=_db.jdump(cloids)):
                cmap[str(r["cloid"])] = str(r["subscription_id"])
        att = attribute_fills(raw, trading_address=addr, cloid_to_subscription=cmap, windows=_windows(addr, subs))
        report.foreign += len(att.foreign)
        rows: list[dict[str, Any]] = []
        bad_coin: list[Fill] = []
        for a in att.attributed:
            (rows.append(_fill_row(a.fill, a.subscription_id, a.via)) if _COIN_RE.fullmatch(a.fill.coin)
             else bad_coin.append(a.fill))
        for f in att.ours_unmatched:
            (rows.append(_fill_row(f, None, None)) if _COIN_RE.fullmatch(f.coin) else bad_coin.append(f))
        inserted: set[int] = set()
        if rows:
            inserted = {int(r["tid"]) for r in _db.rows(conn, _INSERT_FILLS, addr=addr, rows=_db.jdump(rows))}
        report.inserted += len(inserted)
        new_att = [a for a in att.attributed if a.fill.tid in inserted]
        report.attributed += len(new_att)
        for f in att.ours_unmatched:
            if f.tid in inserted:
                report.unattributed += 1
                _db.ops_alert(conn, "fill_unattributed", {
                    "coin": f.coin, "tid": f.tid, "cloid": f.cloid, "time_ms": f.time_ms,
                    "builder_fee_micro": f.builder_fee_micro, "address": _db.short_addr(addr)},
                    severity="warn", dedup_key=f"fill_unattributed:{addr}:{f.tid}")
        for raw_fill, reason in att.rejected:
            if not is_platform_cloid(raw_fill.get("cloid")):
                continue                                     # someone else's unparseable fill: not our business
            report.rejected += 1
            _db.ops_alert(conn, "fill_rejected", {"reason": reason[:120], "tid": raw_fill.get("tid"),
                                                  "coin": str(raw_fill.get("coin"))[:40],
                                                  "address": _db.short_addr(addr)},
                          severity="warn", dedup_key=f"fill_rejected:{addr}:{raw_fill.get('tid')}")
        for f in bad_coin:
            report.rejected += 1
            _db.ops_alert(conn, "fill_rejected", {"reason": "coin name outside the DB format", "tid": f.tid,
                                                  "coin": f.coin[:40], "address": _db.short_addr(addr)},
                          severity="warn", dedup_key=f"fill_rejected:{addr}:{f.tid}")
        for a in new_att:
            sub = by_id.get(a.subscription_id)
            pc = sub.get("pnl_cursor_ms") if sub else None
            if pc is not None and a.fill.time_ms <= int(pc):
                report.late_fills += 1
                _db.ops_alert(conn, "fill_after_settlement", {
                    "subscription_id": a.subscription_id, "tid": a.fill.tid, "time_ms": a.fill.time_ms,
                    "pnl_cursor_ms": int(pc), "net_pnl_micro": a.fill.net_pnl_micro},
                    severity="critical", dedup_key=f"fill_after_settlement:{addr}:{a.fill.tid}")
        for sub_id, kind, payload in trade_events(new_att):
            sub = by_id.get(sub_id) or {}
            payload["strategy_id"] = sub.get("strategy_id")
            if _db.emit_event(conn, kind=kind, payload=payload, user_id=sub.get("user_id"), severity="info",
                              dedup_key=f"{kind}:{sub_id}:{payload['order']}"):
                report.events += 1
        if complete:
            new_cur = max(max_time or 0, now_ms - overlap_ms)
        else:
            report.incomplete += 1
            new_cur = max_time if max_time is not None else cur
        _db.set_cursor(conn, JOB, addr, new_cur, {"last_run_ms": now_ms, "complete": complete})
