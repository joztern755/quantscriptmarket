"""``fills_ingest(db, now)`` — ``/internal/fills-ingest`` (SPEC §1.1, §12 trade alerts).

For every trading address with a live (pending|active|past_due|reduce_only|paused_user|closing) or recently cancelled
subscription: ``userFillsByTime`` since the address cursor (minus an overlap; inserts are idempotent on
UNIQUE(trading_address, tid)) → ``app.hl.fills.attribute_fills`` (orders.cloid → subscription; subscription windows as
the crash fallback) → ``fills`` rows with ``net_pnl_micro = floor((closedPnl − fee) × 1e6)`` → trade events in
``events_outbox``.

Which fills are stored: only fills carrying OUR cloid prefix (attributed, or ``subscription_id`` NULL + ops event when no
subscription matches). The user's own trades are never stored: their ``builderFee`` may belong to another builder and
settlement recognises every stored fill's builder fee as ours.

Attribution (REVIEW_MONEY H2): a fill is ours only when its cloid is the cloid of an order we recorded for a
subscription on this address AND its exchange-assigned oid equals the oid we recorded (an order without a recorded oid
— crash window — gets the fill's oid here, ``fills.oid_verified``). No time-window fallback: a platform-prefixed fill
that matches no recorded order is stored unattributed (ops alert ``fill_unattributed``) and treated as NOT ours; a
recorded cloid with another oid is a forgery (critical ``fill_oid_mismatch``) and is not stored.

Position book (REVIEW_MONEY H1; ``app.domain.profit_share.PositionBook``, table ``subscription_positions``): under a
per-address advisory lock, the new fills of this batch are applied in (time, tid) order to the subscription's book:
our fills realise PnL against OUR average entry (``fills.book_pnl_micro`` = realised − fee; Hyperliquid's
account-level ``closedPnl`` stays in ``net_pnl_micro`` for display and cross-checks only). Every fill on a strategy
coin that is NOT ours (manual trade, other app, forged prefixed cloid) inside a subscription's window marks the book
to market at the fill price and takes the closed quantity out of it (``subscription_pnl_events`` kind
``foreign_fill``; user + ops alert). A subscription found ``paused_user`` or cancelled ("leave") with an open book is
marked to market at the Hyperliquid mark price once per status change (``mtm_pause`` / ``mtm_leave``). The executor
keeps sizing from the on-chain position; only PnL attribution uses the book.

PnL contract with settlement (``app.execution.settlement``): this job does NOT touch ``subscriptions.cum_pnl_micro`` /
``pnl_cursor``. Settlement CLAIMS (``SettlementRepo.claim_pnl``) every not-yet-settled fill (``book_pnl_micro``),
funding event and book adjustment with time ≤ its cut-off (REVIEW_MONEY M3). A fill ingested after its day was
settled is therefore booked into the NEXT settlement (ops event ``fill_after_settlement``, warn) — never lost.

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

from app.domain.profit_share import PositionBook
from app.errors import AppError
from app.hl.client import is_platform_cloid
from app.hl.fills import AttributedFill, Fill, SubscriptionWindow, attribute_fills
from app.jobs_data import _db
from app.jobs_data.hl import BASE_WEIGHT, WeightPacer, list_weight, make_info_client, make_pacer
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
    oid_mismatch: int = 0
    foreign_book_events: int = 0
    mtm_events: int = 0
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
               {_db.ts_to_ms('s.status_changed_at')} AS status_changed_ms,
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


def _fill_row(f: Fill, sub_id: Optional[str], via: Optional[str], *, book_pnl: Optional[int] = None,
              oid_verified: bool = False) -> dict[str, Any]:
    return {"sub": sub_id, "coin": f.coin, "tid": f.tid, "oid": f.oid, "px": format(f.px, "f"), "sz": format(f.sz, "f"),
            "side": "buy" if f.side == "B" else "sell", "cpm": f.closed_pnl_micro, "fm": f.fee_micro,
            "bfm": f.builder_fee_micro, "npm": f.net_pnl_micro, "cloid": f.cloid, "t": f.time_ms,
            "raw": dict(f.raw), "via": via, "bpm": book_pnl, "ov": bool(oid_verified)}


_INSERT_FILLS = f"""
    INSERT INTO fills (subscription_id, trading_address, coin, tid, oid, px, sz, side, closed_pnl_micro, fee_micro,
                       builder_fee_micro, net_pnl_micro, cloid, time, raw, attributed_via, book_pnl_micro, oid_verified)
    SELECT CAST(r.sub AS uuid), :addr, r.coin, r.tid, r.oid, CAST(r.px AS numeric), CAST(r.sz AS numeric),
           CAST(r.side AS order_side), r.cpm, r.fm, r.bfm, r.npm, r.cloid, {_db.ms_to_ts('r.t')}, r.raw, r.via, r.bpm,
           coalesce(r.ov, false)
      FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(sub text, coin text, tid bigint, oid bigint, px text, sz text,
                                                          side text, cpm bigint, fm bigint, bfm bigint, npm bigint,
                                                          cloid text, t bigint, raw jsonb, via text, bpm bigint,
                                                          ov boolean)
    ON CONFLICT (trading_address, tid) DO NOTHING
    RETURNING tid"""


# ------------------------------------------------------------------------------------------------ position books (H1)
def _dec(v: Any) -> Decimal:
    return Decimal(str(v)) if v is not None else ZERO


class _Books:
    """The subscription position books touched by one address batch (loaded lazily, written back at the end).
    A book that does not exist yet is initialised by replaying the subscription's stored fills of that coin (fills
    ingested before the book existed); their PnL was already settled from Hyperliquid's values."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.books: dict[tuple[str, str], PositionBook] = {}
        self.realized: dict[tuple[str, str], int] = {}
        self.dirty: set[tuple[str, str]] = set()

    def get(self, sub_id: str, coin: str) -> PositionBook:
        key = (sub_id, coin)
        if key in self.books:
            return self.books[key]
        r = _db.one(self.conn, """SELECT qty::text AS qty, avg_px::text AS avg_px FROM subscription_positions
                                   WHERE subscription_id = CAST(:s AS uuid) AND coin = CAST(:c AS text)""",
                    s=sub_id, c=coin)
        if r is not None:
            book = PositionBook(_dec(r["qty"]), _dec(r["avg_px"]))
        else:
            book = PositionBook()
            for f in _db.rows(self.conn, """SELECT sz::text AS sz, px::text AS px, side::text AS side FROM fills
                                             WHERE subscription_id = CAST(:s AS uuid) AND coin = CAST(:c AS text)
                                             ORDER BY time, tid""", s=sub_id, c=coin):
                sz = _dec(f["sz"])
                book = book.own_fill(sz if f["side"] == "buy" else -sz, _dec(f["px"])).book
            self.dirty.add(key)
        self.books[key] = book
        self.realized.setdefault(key, 0)
        return book

    def put(self, sub_id: str, coin: str, book: PositionBook, pnl_micro: int) -> None:
        key = (sub_id, coin)
        self.books[key] = book
        self.realized[key] = self.realized.get(key, 0) + int(pnl_micro)
        self.dirty.add(key)

    def save(self) -> None:
        for (sub_id, coin) in sorted(self.dirty):
            b = self.books[(sub_id, coin)]
            _db.rows(self.conn, """
                INSERT INTO subscription_positions (subscription_id, coin, qty, avg_px, realized_micro, updated_at)
                VALUES (CAST(:s AS uuid), CAST(:c AS text), CAST(:q AS numeric), CAST(:a AS numeric),
                        CAST(:r AS bigint), now())
                ON CONFLICT (subscription_id, coin) DO UPDATE
                   SET qty = EXCLUDED.qty, avg_px = EXCLUDED.avg_px,
                       realized_micro = subscription_positions.realized_micro + EXCLUDED.realized_micro,
                       updated_at = now()
                RETURNING subscription_id""", s=sub_id, c=coin, q=format(b.qty, "f"), a=format(b.avg_px, "f"),
                     r=int(self.realized.get((sub_id, coin), 0)))
        self.dirty.clear()
        self.realized = {k: 0 for k in self.realized}


def _book_event(conn: Any, *, sub_id: str, coin: str, kind: str, ref: str, time_ms: int, px: Optional[Decimal],
                before: PositionBook, after: PositionBook, pnl_micro: int, detail: Mapping[str, Any]) -> bool:
    r = _db.one(conn, f"""
        INSERT INTO subscription_pnl_events (subscription_id, coin, kind, ref, time, px, qty_before, avg_before,
                                             qty_after, avg_after, pnl_micro, detail)
        VALUES (CAST(:s AS uuid), CAST(:c AS text), CAST(:k AS text), CAST(:r AS text), {_db.ms_to_ts('CAST(:t AS bigint)')},
                CAST(:px AS numeric), CAST(:qb AS numeric), CAST(:ab AS numeric), CAST(:qa AS numeric),
                CAST(:aa AS numeric), CAST(:pnl AS bigint), CAST(:d AS jsonb))
        ON CONFLICT (subscription_id, kind, ref) DO NOTHING
        RETURNING id""", s=sub_id, c=coin, k=kind, r=ref[:200], t=int(time_ms),
        px=format(px, "f") if px is not None else None, qb=format(before.qty, "f"), ab=format(before.avg_px, "f"),
        qa=format(after.qty, "f"), aa=format(after.avg_px, "f"), pnl=int(pnl_micro), d=_db.jdump(dict(detail)))
    return r is not None


def _covering_sub(subs: Sequence[Mapping[str, Any]], coin: str, t_ms: int) -> Optional[Mapping[str, Any]]:
    """The subscription whose window (created ≤ t < ended) and markets cover a foreign fill (one live per address)."""
    hits = [s for s in subs if coin in s["markets"] and int(s["created_ms"]) <= t_ms
            and (s.get("ended_ms") is None or t_ms < int(s["ended_ms"]))]
    return hits[-1] if hits else None


def _apply_books(conn: Any, addr: str, subs: Sequence[Mapping[str, Any]], ours: Sequence[AttributedFill],
                 not_ours: Sequence[Fill], reduce_only: Mapping[str, bool], report: FillsReport) -> dict[int, int]:
    """Apply this batch's NEW fills to the books in (time, tid) order. Returns {tid: book PnL micro} for our fills."""
    books = _Books(conn)
    seen_foreign: set[str] = set()
    if not_ours:
        tids = sorted({str(f.tid) for f in not_ours})
        for r in _db.rows(conn, """SELECT ref FROM subscription_pnl_events e JOIN subscriptions s ON s.id = e.subscription_id
                                    WHERE s.trading_address = :a AND e.kind = 'foreign_fill'
                                      AND e.ref IN (SELECT jsonb_array_elements_text(CAST(:t AS jsonb)))""",
                          a=addr, t=_db.jdump(tids)):
            seen_foreign.add(str(r["ref"]))
    items: list[tuple[int, int, int, Any]] = [(a.fill.time_ms, a.fill.tid, 0, a) for a in ours]
    items += [(f.time_ms, f.tid, 1, f) for f in not_ours if str(f.tid) not in seen_foreign]
    out: dict[int, int] = {}
    for _t, _tid, is_foreign, item in sorted(items, key=lambda x: (x[0], x[1], x[2])):
        if not is_foreign:
            a: AttributedFill = item
            f = a.fill
            before = books.get(a.subscription_id, f.coin)
            step = before.own_fill(f.signed_sz, f.px, f.fee, reduce_only=bool(reduce_only.get(f.cloid or "", False)))
            out[f.tid] = step.pnl_micro
            books.put(a.subscription_id, f.coin, step.book, step.pnl_micro)
            if step.excess_qty > 0:
                _book_event(conn, sub_id=a.subscription_id, coin=f.coin, kind="excess_reduce", ref=str(f.tid),
                            time_ms=f.time_ms, px=f.px, before=before, after=step.book, pnl_micro=0,
                            detail={"excess_qty": format(step.excess_qty, "f")})
            continue
        f = item
        sub = _covering_sub(subs, f.coin, f.time_ms)
        if sub is None or not f.is_perp:
            continue
        before = books.get(sub["id"], f.coin)
        step = before.foreign_fill(f.signed_sz, f.px)
        if not _book_event(conn, sub_id=sub["id"], coin=f.coin, kind="foreign_fill", ref=str(f.tid), time_ms=f.time_ms,
                           px=f.px, before=before, after=step.book, pnl_micro=step.pnl_micro,
                           detail={"side": f.side, "sz": format(f.sz, "f"), "closed_qty": format(step.closed_qty, "f"),
                                   "prefixed_cloid": is_platform_cloid(f.cloid)}):
            continue
        books.put(sub["id"], f.coin, step.book, step.pnl_micro)
        report.foreign_book_events += 1
        if step.closed_qty > 0 or step.pnl_micro != 0:
            payload = {"subscription_id": sub["id"], "strategy_id": sub.get("strategy_id"), "coin": f.coin,
                       "side": "buy" if f.side == "B" else "sell", "size": _fmt(f.sz), "px": _fmt(f.px),
                       "closed_qty": _fmt(step.closed_qty), "attributed_pnl_micro": step.pnl_micro,
                       "time_ms": f.time_ms}
            _db.emit_event(conn, kind="foreign_trade_detected", payload=payload, user_id=sub.get("user_id"),
                           severity="warn", dedup_key=f"foreign_trade:{sub['id']}:{f.tid}")
            _db.ops_alert(conn, "foreign_trade_on_strategy_coin", payload, severity="warn",
                          dedup_key=f"foreign_trade:{addr}:{f.tid}")
    books.save()
    return out


def _mark_px(info: Any, pacer: Optional[WeightPacer], coin: str, cache: dict[str, Decimal]) -> Optional[Decimal]:
    """Hyperliquid mark price of ``coin`` (validator or builder dex), or None when unavailable."""
    if coin in cache:
        return cache[coin]
    from app.hl.markets import MarketCatalog

    dexes = MarketCatalog.dexes_for([coin])
    try:
        if pacer is not None:
            pacer.spend(BASE_WEIGHT * (2 + len(dexes)))
        cat = MarketCatalog.from_info(info, dexes)
        px = cat.ctx(coin).mark_px
    except Exception:  # noqa: BLE001 - retried by the next run (the event is only recorded with a price)
        _db.log.warning("mark_px_unavailable", extra={"fields": {"coin": coin}})
        return None
    if px is None or px <= 0:
        return None
    cache[coin] = px
    return px


def _mtm_status_changes(conn: Any, info: Any, pacer: Optional[WeightPacer], addr: str,
                        subs: Sequence[Mapping[str, Any]], now_ms: int, recent_cancel_days: int,
                        report: FillsReport) -> None:
    """REVIEW_MONEY H1: a subscription that is ``paused_user`` or cancelled ("leave") with an open book is marked to
    market at the mark price, once per status change (event ref = status epoch + coin). Pause: the average entry steps
    to the mark (later closes are charged only on the move after it). Leave: the book is closed (the position is the
    user's own from now on)."""
    if info is None:
        return
    cache: dict[str, Decimal] = {}
    for s in subs:
        if s["status"] == "paused_user":
            kind, epoch = "mtm_pause", s.get("status_changed_ms")
        elif s["status"] == "cancelled" and _in_scope(s, now_ms, recent_cancel_days):
            kind, epoch = "mtm_leave", s.get("ended_ms")
        else:
            continue
        if epoch is None:
            continue
        open_books = _db.rows(conn, """SELECT coin, qty::text AS qty, avg_px::text AS avg_px FROM subscription_positions
                                        WHERE subscription_id = CAST(:s AS uuid) AND qty <> 0 ORDER BY coin""", s=s["id"])
        for b in open_books:
            ref = f"{int(epoch)}:{b['coin']}"
            if _db.one(conn, """SELECT 1 AS x FROM subscription_pnl_events WHERE subscription_id = CAST(:s AS uuid)
                                   AND kind = CAST(:k AS text) AND ref = CAST(:r AS text)""",
                       s=s["id"], k=kind, r=ref) is not None:
                continue
            px = _mark_px(info, pacer, str(b["coin"]), cache)
            if px is None:
                continue
            before = PositionBook(_dec(b["qty"]), _dec(b["avg_px"]))
            step = before.mark_to_market(px)
            after = PositionBook() if kind == "mtm_leave" else step.book
            if not _book_event(conn, sub_id=s["id"], coin=str(b["coin"]), kind=kind, ref=ref, time_ms=now_ms, px=px,
                               before=before, after=after, pnl_micro=step.pnl_micro,
                               detail={"status": s["status"], "status_epoch_ms": int(epoch)}):
                continue
            _db.rows(conn, """UPDATE subscription_positions
                                 SET qty = CAST(:q AS numeric), avg_px = CAST(:a AS numeric),
                                     realized_micro = realized_micro + CAST(:p AS bigint), updated_at = now()
                               WHERE subscription_id = CAST(:s AS uuid) AND coin = CAST(:c AS text)
                               RETURNING coin""", q=format(after.qty, "f"), a=format(after.avg_px, "f"),
                     p=int(step.pnl_micro), s=s["id"], c=str(b["coin"]))
            report.mtm_events += 1


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
            _store_address(db, addr, subs, raw, complete, cur, now_ms=now_ms, overlap_ms=overlap_ms, report=report,
                           info=info, pacer=pacer, recent_cancel_days=recent_cancel_days)
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
                   cur: Optional[int], *, now_ms: int, overlap_ms: int, report: FillsReport, info: Any = None,
                   pacer: Optional[WeightPacer] = None, recent_cancel_days: int = 7) -> None:
    report.fetched += len(raw)
    by_id = {s["id"]: s for s in subs}
    cloids = sorted({str(f.get("cloid")).lower() for f in raw if is_platform_cloid(f.get("cloid"))})
    max_time = max((int(f["time"]) for f in raw if isinstance(f.get("time"), int)), default=None)
    with _db.transaction(db) as conn:
        # one writer per trading address: the position books must see this address's fills exactly once, in order
        _db.rows(conn, "SELECT pg_advisory_xact_lock(hashtextextended(CAST(:k AS text), 0)) AS l",
                 k=f"aijalon:fills:{addr}")
        cmap: dict[str, str] = {}
        omap: dict[str, Optional[int]] = {}
        ro_map: dict[str, bool] = {}
        if cloids:
            for r in _db.rows(conn, """
                    SELECT o.cloid, o.subscription_id::text AS subscription_id, o.oid, o.reduce_only
                      FROM orders o JOIN subscriptions s ON s.id = o.subscription_id
                     WHERE s.trading_address = :addr
                       AND o.cloid IN (SELECT jsonb_array_elements_text(CAST(:cl AS jsonb)))""",
                              addr=addr, cl=_db.jdump(cloids)):
                cmap[str(r["cloid"])] = str(r["subscription_id"])
                omap[str(r["cloid"])] = int(r["oid"]) if r.get("oid") is not None else None
                ro_map[str(r["cloid"])] = bool(r.get("reduce_only"))
        att = attribute_fills(raw, trading_address=addr, cloid_to_subscription=cmap, windows=_windows(addr, subs),
                              cloid_to_oid=omap)
        report.foreign += len(att.foreign)
        # which fills are new (the fetch overlaps the previous run)
        all_tids = sorted({f.tid for f in [a.fill for a in att.attributed] + att.ours_unmatched})
        existing: set[int] = set()
        if all_tids:
            existing = {int(r["tid"]) for r in _db.rows(conn, """
                SELECT tid FROM fills WHERE trading_address = :addr
                   AND tid IN (SELECT CAST(jsonb_array_elements_text(CAST(:t AS jsonb)) AS bigint))""",
                addr=addr, t=_db.jdump(all_tids))}
        bad_coin: list[Fill] = []
        new_ours = [a for a in att.attributed if a.fill.tid not in existing and _COIN_RE.fullmatch(a.fill.coin)]
        bad_coin += [a.fill for a in att.attributed if not _COIN_RE.fullmatch(a.fill.coin)]
        not_ours = [f for f in att.not_ours() if f.is_perp and _COIN_RE.fullmatch(f.coin)
                    and f.tid not in existing]
        book_pnl = _apply_books(conn, addr, subs, new_ours, not_ours, ro_map, report)
        rows: list[dict[str, Any]] = []
        for a in new_ours:
            rows.append(_fill_row(a.fill, a.subscription_id, a.via, book_pnl=book_pnl.get(a.fill.tid),
                                  oid_verified=a.oid_verified))
        for f in att.ours_unmatched:
            (rows.append(_fill_row(f, None, None)) if _COIN_RE.fullmatch(f.coin) else bad_coin.append(f))
        inserted: set[int] = set()
        if rows:
            inserted = {int(r["tid"]) for r in _db.rows(conn, _INSERT_FILLS, addr=addr, rows=_db.jdump(rows))}
        report.inserted += len(inserted)
        new_att = [a for a in new_ours if a.fill.tid in inserted]
        report.attributed += len(new_att)
        # H2: remember the exchange-assigned oid of an order that had none recorded (crash window)
        for a in new_att:
            if a.oid_verified and omap.get(a.fill.cloid or "") is None:
                _db.rows(conn, """UPDATE orders SET oid = CAST(:oid AS bigint)
                                   WHERE cloid = CAST(:c AS text) AND oid IS NULL RETURNING cloid""",
                         oid=a.fill.oid, c=a.fill.cloid)
                omap[a.fill.cloid or ""] = a.fill.oid
        for f in att.ours_unmatched:
            if f.tid in inserted:
                report.unattributed += 1
                _db.ops_alert(conn, "fill_unattributed", {
                    "coin": f.coin, "tid": f.tid, "cloid": f.cloid, "time_ms": f.time_ms,
                    "builder_fee_micro": f.builder_fee_micro, "address": _db.short_addr(addr),
                    "window_subscription_id": att.window_hints.get(f.tid)},
                    severity="warn", dedup_key=f"fill_unattributed:{addr}:{f.tid}")
        for f in att.oid_mismatch:
            report.oid_mismatch += 1
            _db.ops_alert(conn, "fill_oid_mismatch", {
                "coin": f.coin, "tid": f.tid, "cloid": f.cloid, "oid": f.oid, "recorded_oid": omap.get(f.cloid or ""),
                "address": _db.short_addr(addr)}, severity="critical", dedup_key=f"fill_oid_mismatch:{addr}:{f.tid}")
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
                # REVIEW_MONEY M3: not lost — settlement claims every unclaimed fill, so it is booked next settlement
                report.late_fills += 1
                _db.ops_alert(conn, "fill_after_settlement", {
                    "subscription_id": a.subscription_id, "tid": a.fill.tid, "time_ms": a.fill.time_ms,
                    "pnl_cursor_ms": int(pc), "net_pnl_micro": a.fill.net_pnl_micro,
                    "book_pnl_micro": book_pnl.get(a.fill.tid), "booked_into": "next settlement"},
                    severity="warn", dedup_key=f"fill_after_settlement:{addr}:{a.fill.tid}")
        for sub_id, kind, payload in trade_events(new_att):
            sub = by_id.get(sub_id) or {}
            payload["strategy_id"] = sub.get("strategy_id")
            if _db.emit_event(conn, kind=kind, payload=payload, user_id=sub.get("user_id"), severity="info",
                              dedup_key=f"{kind}:{sub_id}:{payload['order']}"):
                report.events += 1
        _mtm_status_changes(conn, info, pacer, addr, subs, now_ms, recent_cancel_days, report)
        if complete:
            new_cur = max(max_time or 0, now_ms - overlap_ms)
        else:
            report.incomplete += 1
            new_cur = max_time if max_time is not None else cur
        _db.set_cursor(conn, JOB, addr, new_cur, {"last_run_ms": now_ms, "complete": complete})
