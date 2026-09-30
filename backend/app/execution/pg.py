"""Postgres implementations of every execution port (SQLAlchemy Core ``text()`` + psycopg 3, via ``app.db.engine``).

Conventions (same as ``app.api.store`` / ``app.db.repositories.ledger``)
- Every value is a bound parameter (``:name``); every bind is wrapped in ``CAST(:x AS type)`` so psycopg's
  server-side binding never has to guess, and each name is used with one type per statement.
- Numerics (sizes, prices) are selected as text and parsed to ``Decimal``; uuids as text; money is bigint micro-USD.
- No ``SELECT *`` on ``agent_keys`` / ``strategy_versions`` (column privileges).

Transactions — ``PgDatabase`` wraps either
- a ``DatabasePort`` (``begin()`` → SQLAlchemy Connection in a transaction; prod: ``app.api.adapters.SqlDatabase``
  or ``jobs.ExecutorDatabase``), or
- a ``SqlRunner`` (``fetchall``; the psql-backed runner of the DB tests), optionally with ``atomic()`` /
  ``session()`` context managers (tests provide real transactions through an interactive psql session).

Outside ``atomic()`` every statement commits on its own. That is REQUIRED by the executor: an order intent row
(``insert_order``) must be durable before the order is sent. ``atomic()`` (the settlement ``UnitOfWork``) makes
every statement issued inside it — repos and ledger alike, on the same thread — one transaction.

Advisory locks (``PgLockProvider``) use ``pg_try_advisory_xact_lock`` on a DEDICATED transaction held open for
the lock's lifetime (``session()``): released at commit/rollback or when the connection dies, never leaked into
the connection pool. Other statements run on other connections meanwhile.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Mapping, Sequence

from app.config import Economics
from app.hl.client import CLOID_PREFIX
from app.logging import get_logger

from .ports import (
    CLOSING_STATUS,
    TRADABLE_STATUSES,
    BarSignal,
    BuilderFeeFill,
    ExpectedPosition,
    Flags,
    OrderRecord,
    PlanAccount,
    PnlDelta,
    SettlementSubscription,
    SubscriptionView,
)

__all__ = [
    "PgDatabase", "PgSubscriptionRepo", "PgSignalRepo", "PgFlagRepo", "PgLockProvider", "PgSettlementRepo",
    "PgReconcileRepo", "PgReferralLookup", "PgUnitOfWork", "PgLedger", "PgAlertRepo", "PgMarketPauseFlags",
    "PgContactDirectory", "PgReconciliationStore", "PgReferralTierRepo", "PgCreatorSignalRepo",
    "as_datetime", "as_bytes", "json_dumps",
]

log = get_logger("app.execution.pg")

_TRADABLE_SQL = "('active', 'past_due', 'reduce_only')"
assert tuple(x.strip(" '") for x in _TRADABLE_SQL.strip("()").split(",")) == TRADABLE_STATUSES
# Fills carrying our builder code are exactly the fills of orders with our cloid prefix (SPEC §1.1).
_OUR_CLOID_LIKE = f"0x{CLOID_PREFIX}%"


# ---------------------------------------------------------------------------------------------------------- helpers

def as_datetime(v: Any) -> datetime | None:
    """timestamptz from psycopg (datetime) or from the JSON test runner (ISO string) → aware UTC datetime."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def as_date(v: Any) -> date | None:
    if v is None or isinstance(v, date) and not isinstance(v, datetime):
        return v
    return date.fromisoformat(str(v)[:10])


def as_bytes(v: Any) -> bytes | None:
    """bytea from psycopg (bytes / memoryview) or from the JSON test runner ("\\x…" hex text)."""
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    s = str(v)
    if s.startswith("\\x"):
        return bytes.fromhex(s[2:])
    raise ValueError("unexpected bytea representation")


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def _ts(dt: datetime | None) -> str | None:
    """Bind a timestamp as ISO text (+CAST … AS timestamptz in SQL): identical for psycopg and the psql runner."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat()


def _markets(v: Any) -> tuple[str, ...]:
    v = _json(v)
    return tuple(str(x) for x in (v or ()))


# ---------------------------------------------------------------------------------------------------------- database

class PgDatabase:
    """The one DB handle every Pg* adapter uses. Itself a ``SqlRunner`` (``fetchall``) so ``app.ledger.service``
    (``BoundLedger`` / ``PostgresLedgerStore``) can run on it and join the current ``atomic()`` transaction."""

    def __init__(self, db: Any) -> None:
        if isinstance(db, PgDatabase):
            db = db._db
        if hasattr(db, "execute") and not hasattr(db, "fetchall"):
            # an open SQLAlchemy Connection handed in by the API (inside the caller's transaction)
            from app.db.engine import SqlAlchemyRunner

            db = SqlAlchemyRunner(db)
        if not (hasattr(db, "fetchall") or hasattr(db, "begin")):
            raise TypeError("PgDatabase needs a DatabasePort (begin()) or a SqlRunner (fetchall())")
        self._db = db
        self._local = threading.local()

    @property
    def raw(self) -> Any:
        return self._db

    def _current(self) -> Any:
        return getattr(self._local, "runner", None)

    @staticmethod
    def _sa(conn: Any) -> Any:
        if hasattr(conn, "fetchall"):
            return conn
        from app.db.engine import SqlAlchemyRunner

        return SqlAlchemyRunner(conn)

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        cur = self._current()
        if cur is not None:
            return [dict(r) for r in cur.fetchall(sql, params)]
        if hasattr(self._db, "fetchall"):
            return [dict(r) for r in self._db.fetchall(sql, params)]
        with self._db.begin() as conn:
            return [dict(r) for r in self._sa(conn).fetchall(sql, params)]

    def one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def all(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        return self.fetchall(sql, params)

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """One transaction for everything issued on this thread inside the block (nested blocks join it)."""
        if self._current() is not None:
            yield
            return
        if hasattr(self._db, "fetchall"):
            opener = getattr(self._db, "atomic", None)
            if opener is None:   # plain autocommit runner (tests only): no transaction to open
                yield
                return
            ctx = opener()
        else:
            ctx = self._db.begin()
        with ctx as conn:
            self._local.runner = self._sa(conn) if conn is not None else self._db
            try:
                yield
            finally:
                self._local.runner = None

    @contextmanager
    def session(self) -> Iterator[Any]:
        """A dedicated transaction NOT shared with the thread's other statements (advisory xact locks)."""
        if hasattr(self._db, "fetchall"):
            opener = getattr(self._db, "session", None)
            if opener is None:
                yield self._db    # autocommit runner (tests): each call is its own session
                return
            with opener() as runner:
                yield runner
            return
        with self._db.begin() as conn:
            yield self._sa(conn)

    def savepoint(self) -> Any:
        """Used by ``PostgresLedgerStore.insert_tx``: a failed ledger post must not poison the caller's tx."""
        cur = self._current()
        sp = getattr(cur, "savepoint", None) if cur is not None else None
        return sp() if sp is not None else nullcontext()

    def table_exists(self, name: str) -> bool:
        row = self.one("SELECT to_regclass(CAST(:n AS text)) IS NOT NULL AS ok", n=f"public.{name}")
        return bool(row and row["ok"])


class PgUnitOfWork:
    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def atomic(self) -> Any:
        return self.db.atomic()


# ---------------------------------------------------------------------------------------------------------- locks

class PgLockProvider:
    """``LockProvider``: non-blocking advisory lock scoped to a dedicated transaction (see module doc)."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    @contextmanager
    def try_lock(self, key: str) -> Iterator[bool]:
        with self.db.session() as runner:
            rows = runner.fetchall("SELECT pg_try_advisory_xact_lock(hashtextextended(CAST(:k AS text), 0)) AS ok",
                                   {"k": "aijalon:" + key})
            yield bool(rows and rows[0]["ok"])


# ---------------------------------------------------------------------------------------------------------- execution

_SUB_VIEW_COLS = """
    s.id::text AS id, s.user_id::text AS user_id, s.strategy_id::text AS strategy_id,
    s.strategy_version_id::text AS strategy_version_id, s.master_address, s.trading_address, s.allocation_micro,
    s.max_leverage_x100, s.status::text AS status, coalesce(v.markets, st.markets) AS markets,
    s.consecutive_rejections, v.max_leverage AS version_max_leverage, s.past_due_since"""
_SUB_VIEW_FROM = """
    FROM subscriptions s
    JOIN strategies st ON st.id = s.strategy_id
    JOIN strategy_versions v ON v.id = s.strategy_version_id"""

_ORDER_COLS = """
    subscription_id::text AS subscription_id, strategy_version_id::text AS strategy_version_id, bar_close, attempt,
    cloid, coin, side::text AS side, sz::text AS sz, limit_px::text AS limit_px, reduce_only, status::text AS status,
    jitter_seconds, submitted_at, filled_sz::text AS filled_sz, avg_px::text AS avg_px, oid, error, hl_response,
    created_at"""


def _sub_view(r: Mapping[str, Any]) -> SubscriptionView:
    vml = r.get("version_max_leverage")
    return SubscriptionView(
        id=str(r["id"]), user_id=str(r["user_id"]), strategy_id=str(r["strategy_id"]),
        strategy_version_id=str(r["strategy_version_id"]), master_address=str(r["master_address"] or "").lower(),
        trading_address=str(r["trading_address"]).lower(), allocation_micro=int(r["allocation_micro"]),
        max_leverage_x100=int(r["max_leverage_x100"]), status=str(r["status"]), markets=_markets(r["markets"]),
        consecutive_rejections=int(r["consecutive_rejections"] or 0),
        strategy_max_leverage_x100=int(vml) * 100 if vml is not None else None,
        past_due_since=as_datetime(r.get("past_due_since")))


def _order(r: Mapping[str, Any]) -> OrderRecord:
    submitted = as_datetime(r.get("submitted_at")) or as_datetime(r.get("created_at"))
    return OrderRecord(
        subscription_id=str(r["subscription_id"]), strategy_version_id=str(r["strategy_version_id"] or ""),
        bar_close=as_datetime(r["bar_close"]), attempt=int(r["attempt"]), cloid=str(r["cloid"]), coin=str(r["coin"]),
        is_buy=r["side"] == "buy", sz=Decimal(str(r["sz"])), limit_px=Decimal(str(r["limit_px"])),
        reduce_only=bool(r["reduce_only"]), status=str(r["status"]), jitter_seconds=int(r["jitter_seconds"] or 0),
        submitted_at=submitted, filled_sz=_dec(r.get("filled_sz")) or Decimal(0), avg_px=_dec(r.get("avg_px")),
        oid=int(r["oid"]) if r.get("oid") is not None else None, error=r.get("error"),
        hl_response=_json(r.get("hl_response")) or {})


class PgSubscriptionRepo:
    """``SubscriptionRepo`` over subscriptions / orders / subscription_bar_runs / subscription_targets."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def due_subscriptions(self, strategy_version_id: str, bar_close: datetime, limit: int) -> Sequence[SubscriptionView]:
        rows = self.db.all(f"""
            SELECT {_SUB_VIEW_COLS} {_SUB_VIEW_FROM}
             WHERE s.strategy_version_id = CAST(:v AS uuid)
               AND s.status IN {_TRADABLE_SQL}
               AND s.master_address IS NOT NULL
               -- an expired/revoked agent cannot place any order (not even exits): skip until re-approved
               AND EXISTS (SELECT 1 FROM agent_keys k
                            WHERE k.user_id = s.user_id AND k.master_address = s.master_address
                              AND k.status = 'active' AND (k.valid_until IS NULL OR k.valid_until > now()))
               AND NOT EXISTS (SELECT 1 FROM subscription_bar_runs r
                                WHERE r.subscription_id = s.id AND r.bar_close = CAST(:bar AS timestamptz))
             ORDER BY s.id
             LIMIT CAST(:lim AS integer)""", v=strategy_version_id, bar=_ts(bar_close), lim=int(limit))
        return [_sub_view(r) for r in rows]

    def get_subscription(self, subscription_id: str) -> SubscriptionView | None:
        r = self.db.one(f"SELECT {_SUB_VIEW_COLS} {_SUB_VIEW_FROM} WHERE s.id = CAST(:id AS uuid)", id=subscription_id)
        return _sub_view(r) if r else None

    def closing_subscriptions(self, limit: int) -> Sequence[SubscriptionView]:
        rows = self.db.all(f"""
            SELECT {_SUB_VIEW_COLS} {_SUB_VIEW_FROM}
             WHERE s.status = CAST(:st AS subscription_status)
             ORDER BY s.status_changed_at, s.id
             LIMIT CAST(:lim AS integer)""", st=CLOSING_STATUS, lim=int(limit))
        return [_sub_view(r) for r in rows]

    def finish_closing(self, subscription_id: str, now: datetime) -> bool:
        rows = self.db.all("""
            UPDATE subscriptions
               SET status = CAST('cancelled' AS subscription_status), status_changed_at = CAST(:t AS timestamptz),
                   cancelled_at = coalesce(cancelled_at, CAST(:t AS timestamptz))
             WHERE id = CAST(:id AS uuid) AND status = CAST('closing' AS subscription_status)
            RETURNING id::text AS id""", id=subscription_id, t=_ts(now))
        return bool(rows)

    def is_bar_done(self, subscription_id: str, bar_close: datetime) -> bool:
        return self.db.one("""SELECT 1 AS x FROM subscription_bar_runs
                               WHERE subscription_id = CAST(:s AS uuid) AND bar_close = CAST(:b AS timestamptz)""",
                           s=subscription_id, b=_ts(bar_close)) is not None

    def mark_bar_done(self, subscription_id: str, bar_close: datetime, outcome: str) -> None:
        self.db.all("""INSERT INTO subscription_bar_runs (subscription_id, bar_close, outcome)
                       VALUES (CAST(:s AS uuid), CAST(:b AS timestamptz), CAST(:o AS text))
                       ON CONFLICT (subscription_id, bar_close) DO NOTHING""",
                    s=subscription_id, b=_ts(bar_close), o=str(outcome)[:64])

    def orders_for_bar(self, subscription_id: str, bar_close: datetime, coin: str) -> Sequence[OrderRecord]:
        rows = self.db.all(f"""SELECT {_ORDER_COLS} FROM orders
                                WHERE subscription_id = CAST(:s AS uuid) AND bar_close = CAST(:b AS timestamptz)
                                  AND coin = CAST(:c AS text)
                                ORDER BY attempt, created_at""", s=subscription_id, b=_ts(bar_close), c=coin)
        return [_order(r) for r in rows]

    def unresolved_orders(self, subscription_id: str) -> Sequence[OrderRecord]:
        rows = self.db.all(f"""SELECT {_ORDER_COLS} FROM orders
                                WHERE subscription_id = CAST(:s AS uuid)
                                  AND status IN ('submitting', 'unknown', 'resting')
                                ORDER BY bar_close, coin, attempt""", s=subscription_id)
        return [_order(r) for r in rows]

    def get_order(self, cloid: str) -> OrderRecord | None:
        r = self.db.one(f"SELECT {_ORDER_COLS} FROM orders WHERE cloid = CAST(:c AS text)", c=cloid.lower())
        return _order(r) if r else None

    def insert_order(self, record: OrderRecord) -> bool:
        rows = self.db.all("""
            INSERT INTO orders (subscription_id, strategy_version_id, bar_close, attempt, cloid, coin, side, sz,
                                limit_px, reduce_only, status, jitter_seconds, submitted_at)
            VALUES (CAST(:s AS uuid), CAST(:v AS uuid), CAST(:b AS timestamptz), CAST(:a AS integer), CAST(:cl AS text),
                    CAST(:coin AS text), CAST(:side AS order_side), CAST(:sz AS numeric), CAST(:px AS numeric),
                    CAST(:ro AS boolean), CAST(:st AS order_status), CAST(:j AS integer), CAST(:t AS timestamptz))
            ON CONFLICT (cloid) DO NOTHING
            RETURNING id::text AS id""",
            s=record.subscription_id, v=record.strategy_version_id or None, b=_ts(record.bar_close),
            a=int(record.attempt), cl=record.cloid.lower(), coin=record.coin, side="buy" if record.is_buy else "sell",
            sz=str(record.sz), px=str(record.limit_px), ro=bool(record.reduce_only), st=record.status,
            j=int(record.jitter_seconds), t=_ts(record.submitted_at))
        return bool(rows)

    def update_order(self, cloid: str, *, status: str, filled_sz: Decimal, avg_px: Decimal | None, oid: int | None,
                     error: str | None, hl_response: Mapping[str, Any]) -> None:
        self.db.all("""
            UPDATE orders SET status = CAST(:st AS order_status), filled_sz = CAST(:f AS numeric),
                   avg_px = CAST(:p AS numeric), oid = CAST(:oid AS bigint), error = CAST(:e AS text),
                   hl_response = CAST(:r AS jsonb)
             WHERE cloid = CAST(:c AS text)""",
            st=status, f=str(filled_sz or 0), p=str(avg_px) if avg_px is not None and avg_px > 0 else None,
            oid=int(oid) if oid is not None else None, e=(str(error)[:500] if error else None),
            r=json_dumps(dict(hl_response or {})), c=cloid.lower())

    def record_rejection(self, subscription_id: str, reason: str) -> int:
        row = self.db.one("""UPDATE subscriptions SET consecutive_rejections = consecutive_rejections + 1
                              WHERE id = CAST(:s AS uuid)
                             RETURNING consecutive_rejections""", s=subscription_id)
        log.info("rejection_recorded", extra={"fields": {"subscription_id": subscription_id, "reason": reason[:120]}})
        return int(row["consecutive_rejections"]) if row else 0

    def reset_rejections(self, subscription_id: str) -> None:
        self.db.all("""UPDATE subscriptions SET consecutive_rejections = 0
                        WHERE id = CAST(:s AS uuid) AND consecutive_rejections <> 0""", s=subscription_id)

    def record_target(self, subscription_id: str, coin: str, bar_close: datetime, target_notional_micro: int,
                      weight_bps: int) -> None:
        self.db.all("""
            INSERT INTO subscription_targets (subscription_id, coin, bar_close, target_notional_micro, weight_bps)
            VALUES (CAST(:s AS uuid), CAST(:c AS text), CAST(:b AS timestamptz), CAST(:n AS bigint), CAST(:w AS integer))
            ON CONFLICT (subscription_id, coin) DO UPDATE
               SET bar_close = EXCLUDED.bar_close, target_notional_micro = EXCLUDED.target_notional_micro,
                   weight_bps = EXCLUDED.weight_bps, updated_at = now()""",
            s=subscription_id, c=coin, b=_ts(bar_close), n=int(target_notional_micro), w=int(weight_bps))

    # helpers used by jobs (not part of the port)
    def traded_markets(self) -> list[str]:
        rows = self.db.all("""
            SELECT DISTINCT m AS coin FROM (
                SELECT unnest(coalesce(v.markets, st.markets)) AS m
                  FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
                  JOIN strategy_versions v ON v.id = s.strategy_version_id
                 WHERE s.status IN ('active', 'past_due', 'reduce_only', 'closing')) q
            ORDER BY 1""")
        return [str(r["coin"]) for r in rows]


class PgSignalRepo:
    """``SignalRepo``: latest bar_close per listed/paused strategy version that has live subscriptions."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def latest_signals(self) -> Sequence[BarSignal]:
        rows = self.db.all(f"""
            SELECT sg.strategy_version_id::text AS strategy_version_id, sg.bar_close, sg.coin, sg.target_weight_bps,
                   sg.source::text AS source
              FROM strategy_versions v
              JOIN strategies st ON st.id = v.strategy_id
             CROSS JOIN LATERAL (SELECT max(x.bar_close) AS bc FROM signals x WHERE x.strategy_version_id = v.id) m
              JOIN signals sg ON sg.strategy_version_id = v.id AND sg.bar_close = m.bc
             WHERE st.status IN ('listed', 'paused')
               AND EXISTS (SELECT 1 FROM subscriptions s
                            WHERE s.strategy_version_id = v.id AND s.status IN {_TRADABLE_SQL})
             ORDER BY 1, 3""")
        grouped: dict[tuple[str, datetime], dict[str, Any]] = {}
        for r in rows:
            key = (str(r["strategy_version_id"]), as_datetime(r["bar_close"]))
            g = grouped.setdefault(key, {"weights": {}, "sources": set()})
            g["weights"][str(r["coin"])] = int(r["target_weight_bps"])
            g["sources"].add(str(r["source"]))
        return [BarSignal(strategy_version_id=v, bar_close=bc, weights_bps=g["weights"],
                          source=sorted(g["sources"])[0]) for (v, bc), g in grouped.items()]


class PgFlagRepo:
    """``FlagRepo``: one snapshot of system_flags per tick. FAIL CLOSED: any value other than JSON false/null
    counts as engaged (a malformed switch stops trading rather than being ignored)."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def flags(self) -> Flags:
        rows = self.db.all("""SELECT key, (value IS NOT NULL AND value <> CAST('false' AS jsonb)
                                          AND value <> CAST('null' AS jsonb)) AS engaged
                                FROM system_flags""")
        on = {str(r["key"]) for r in rows if r["engaged"]}
        return Flags(
            kill_switch_global="kill_switch_global" in on,
            new_entries_paused="new_entries_paused" in on,
            killed_markets=frozenset(k.split(":", 1)[1] for k in on if k.startswith("kill_switch_market:")),
            paused_entry_markets=frozenset(k.split(":", 1)[1] for k in on if k.startswith("new_entries_paused:")))


# ---------------------------------------------------------------------------------------------------------- alerts

class PgAlertRepo:
    """``app.alerts.notifier.AlertRepo`` (in-app alert rows). The row's dedup_key is the alert key bucketed to
    the notifier's dedupe window, so several executor instances do not duplicate rows, while a recurring
    condition (e.g. a second breaker trip next week) is still recorded."""

    def __init__(self, db: PgDatabase, *, window_s: int = 1800, clock: Any = None) -> None:
        self.db = db
        self.window_s = max(60, int(window_s))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def insert_alert(self, user_id: str | None, severity: str, kind: str, payload: dict) -> None:
        key = str((payload or {}).get("key") or "")[:150]
        bucket = int(self.clock().timestamp()) // self.window_s
        self.db.all("""
            INSERT INTO alerts (user_id, severity, kind, payload, dedup_key)
            VALUES (CAST(:u AS uuid), CAST(:sev AS alert_severity), CAST(:k AS text), CAST(:p AS jsonb),
                    CAST(:d AS text))
            ON CONFLICT DO NOTHING""",
            u=user_id, sev=severity, k=str(kind)[:100], p=json_dumps(payload or {}),
            d=f"{key}@{bucket}" if key else None)


class PgMarketPauseFlags:
    """``app.alerts.notifier.FlagRepo``: critical market alert → ``new_entries_paused:{coin}`` = true (exits keep
    running). Lifting it is maker-checker in the admin API."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def set_market_paused(self, coin: str, reason: str) -> None:
        if not coin:
            raise ValueError("coin required")
        self.db.all("""
            INSERT INTO system_flags (key, value, updated_by) VALUES (CAST(:k AS text), CAST('true' AS jsonb), CAST(:by AS text))
            ON CONFLICT (key) DO UPDATE SET value = CAST('true' AS jsonb), updated_by = EXCLUDED.updated_by
             WHERE system_flags.value <> CAST('true' AS jsonb)""",
            k=f"new_entries_paused:{coin}", by=("system:" + reason)[:200])


class PgContactDirectory:
    """``app.alerts.notifier.ContactDirectory``: the account email (the alerts module may replace this with the
    confirmed alert email once 0007 lands)."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def email_for(self, user_id: str, alert: Any) -> str | None:
        r = self.db.one("SELECT email FROM users WHERE id = CAST(:u AS uuid) AND status = 'active'", u=user_id)
        return (r or {}).get("email") or None


# ---------------------------------------------------------------------------------------------------------- settlement

class PgSettlementRepo:
    """``SettlementRepo``. Status writes are guarded by the current status so a concurrent cancel from the API is
    never overwritten by the billing state machine."""

    def __init__(self, db: PgDatabase, economics: Economics | None = None) -> None:
        self.db = db
        self.economics = economics or Economics()
        self._cols: frozenset[str] | None = None

    def _pnl_columns(self) -> frozenset[str]:
        """Attribution columns added by the data-jobs migration (0006): fills.net_pnl_micro (exact
        floor((closedPnl − fee)·1e6)) and funding_events.attributed_micro (the subscription's share of an account
        payment). Used when present; before 0006 the legacy columns are the only source."""
        if self._cols is None:
            rows = self.db.all("""
                SELECT table_name || '.' || column_name AS c FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND ((table_name = 'fills' AND column_name = 'net_pnl_micro')
                        OR (table_name = 'funding_events' AND column_name = 'attributed_micro'))""")
            self._cols = frozenset(str(r["c"]) for r in rows)
        return self._cols

    def subscriptions_to_settle(self) -> Sequence[SettlementSubscription]:
        rows = self.db.all("""
            SELECT s.id::text AS id, s.user_id::text AS user_id, s.strategy_id::text AS strategy_id,
                   st.owner_user_id::text AS creator_user_id, st.in_house, s.status::text AS status,
                   coalesce(st.profit_share_bps, 0) AS profit_share_bps,
                   coalesce(st.price_monthly_micro, 0) AS price_monthly_micro, s.cum_pnl_micro, s.hwm_micro,
                   s.pnl_cursor, s.current_period_end, s.past_due_since, s.created_at
              FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
             WHERE s.status IN ('active', 'past_due', 'reduce_only', 'paused_user', 'closing')
                OR (s.status = 'cancelled' AND s.cancelled_at IS NOT NULL
                    AND (s.pnl_cursor IS NULL OR s.pnl_cursor <= s.cancelled_at))
             ORDER BY s.created_at, s.id""")
        return [SettlementSubscription(
            id=r["id"], user_id=r["user_id"], strategy_id=r["strategy_id"], creator_user_id=r["creator_user_id"],
            in_house=bool(r["in_house"]), status=r["status"], profit_share_bps=int(r["profit_share_bps"]),
            price_monthly_micro=int(r["price_monthly_micro"]), cum_pnl_micro=int(r["cum_pnl_micro"]),
            hwm_micro=int(r["hwm_micro"]), pnl_cursor=as_datetime(r["pnl_cursor"]),
            current_period_end=as_datetime(r["current_period_end"]), past_due_since=as_datetime(r["past_due_since"]),
            created_at=as_datetime(r["created_at"])) for r in rows]

    def is_settled(self, subscription_id: str, settle_date: date) -> bool:
        return self.db.one("""SELECT 1 AS x FROM profit_share_settlements
                               WHERE subscription_id = CAST(:s AS uuid) AND settle_date = CAST(:d AS date)""",
                           s=subscription_id, d=settle_date.isoformat()) is not None

    def pnl_since(self, subscription_id: str, since: datetime | None, until: datetime) -> PnlDelta:
        """Attributed fills (closedPnl − fee, fee incl. builder fee: app.hl.fills) + attributed funding in
        (since, until]. After a cancellation, funding is only counted up to cancelled_at (SPEC §12 "leave")."""
        cols = self._pnl_columns()
        fill_pnl = ("coalesce(f.net_pnl_micro, f.closed_pnl_micro - f.fee_micro)" if "fills.net_pnl_micro" in cols
                    else "f.closed_pnl_micro - f.fee_micro")
        funding = ("coalesce(e.attributed_micro, 0)" if "funding_events.attributed_micro" in cols else "e.usdc_micro")
        row = self.db.one(f"""
            SELECT (SELECT coalesce(sum({fill_pnl}), 0)::bigint FROM fills f
                     WHERE f.subscription_id = CAST(:s AS uuid)
                       AND (CAST(:since AS timestamptz) IS NULL OR f.time > CAST(:since AS timestamptz))
                       AND f.time <= CAST(:until AS timestamptz)) AS realized,
                   (SELECT coalesce(sum({funding}), 0)::bigint FROM funding_events e
                     WHERE e.subscription_id = CAST(:s AS uuid)
                       AND (CAST(:since AS timestamptz) IS NULL OR e.time > CAST(:since AS timestamptz))
                       AND e.time <= CAST(:until AS timestamptz)
                       AND e.time <= coalesce((SELECT c.cancelled_at FROM subscriptions c WHERE c.id = CAST(:s AS uuid)),
                                              CAST(:until AS timestamptz))) AS funding""",
            s=subscription_id, since=_ts(since), until=_ts(until))
        return PnlDelta(realized_micro=int(row["realized"]), funding_micro=int(row["funding"]), until=until)

    def save_profit_share(self, subscription_id: str, settle_date: date, *, cum_pnl_micro: int, hwm_micro: int,
                          pnl_cursor: datetime, ledger_tx_id: str | None) -> None:
        self.db.all("""
            INSERT INTO profit_share_settlements (subscription_id, settle_date, cum_pnl_micro, hwm_micro, pnl_cursor,
                                                  ledger_tx_id)
            VALUES (CAST(:s AS uuid), CAST(:d AS date), CAST(:c AS bigint), CAST(:h AS bigint), CAST(:t AS timestamptz),
                    CAST(:tx AS uuid))""",
            s=subscription_id, d=settle_date.isoformat(), c=int(cum_pnl_micro), h=int(hwm_micro), t=_ts(pnl_cursor),
            tx=ledger_tx_id or None)
        self.db.all("""UPDATE subscriptions SET cum_pnl_micro = CAST(:c AS bigint), hwm_micro = CAST(:h AS bigint),
                              pnl_cursor = CAST(:t AS timestamptz)
                        WHERE id = CAST(:s AS uuid)""",
                    s=subscription_id, c=int(cum_pnl_micro), h=int(hwm_micro), t=_ts(pnl_cursor))

    def set_status(self, subscription_id: str, status: str, past_due_since: datetime | None) -> None:
        self.db.all("""
            UPDATE subscriptions
               SET status = CAST(:st AS subscription_status), past_due_since = CAST(:p AS timestamptz),
                   status_changed_at = CASE WHEN status = CAST(:st AS subscription_status) THEN status_changed_at
                                            ELSE now() END
             WHERE id = CAST(:s AS uuid) AND status IN ('active', 'past_due', 'reduce_only')""",
            s=subscription_id, st=status, p=_ts(past_due_since))

    def set_period_end(self, subscription_id: str, period_end: datetime) -> None:
        self.db.all("UPDATE subscriptions SET current_period_end = CAST(:e AS timestamptz) WHERE id = CAST(:s AS uuid)",
                    s=subscription_id, e=_ts(period_end))

    def unrecognised_builder_fee_fills(self, until: datetime, limit: int) -> Sequence[BuilderFeeFill]:
        """Only fills of OUR orders (cloid prefix) carry OUR builder fee; a user's own trades through another
        front-end may carry someone else's builder fee — those are marked recognised with nothing posted."""
        rows = self.db.all("""
            SELECT f.tid::text AS tid, f.trading_address, f.subscription_id::text AS subscription_id,
                   s.user_id::text AS user_id, st.owner_user_id::text AS creator_user_id,
                   coalesce(st.in_house, false) AS in_house,
                   CASE WHEN lower(coalesce(f.cloid, '')) LIKE CAST(:pfx AS text) THEN f.builder_fee_micro ELSE 0 END
                       AS builder_fee_micro,
                   f.time
              FROM fills f
              LEFT JOIN subscriptions s ON s.id = f.subscription_id
              LEFT JOIN strategies st ON st.id = s.strategy_id
             WHERE f.builder_fee_recognised_at IS NULL AND f.time <= CAST(:u AS timestamptz)
             ORDER BY f.time, f.trading_address, f.tid
             LIMIT CAST(:lim AS integer)""", pfx=_OUR_CLOID_LIKE, u=_ts(until), lim=int(limit))
        return [BuilderFeeFill(tid=r["tid"], subscription_id=r["subscription_id"], user_id=r["user_id"],
                               creator_user_id=r["creator_user_id"], in_house=bool(r["in_house"]),
                               builder_fee_micro=int(r["builder_fee_micro"]), time=as_datetime(r["time"]),
                               trading_address=str(r["trading_address"]).lower()) for r in rows]

    def mark_builder_fee_recognised(self, trading_address: str, tid: str, ledger_tx_id: str) -> None:
        self.db.all("""
            UPDATE fills SET builder_fee_recognised_at = now(), builder_fee_ledger_tx_id = CAST(:tx AS uuid)
             WHERE trading_address = CAST(:a AS text) AND tid = CAST(:tid AS bigint)
               AND builder_fee_recognised_at IS NULL""",
            a=trading_address.lower(), tid=str(tid), tx=ledger_tx_id or None)

    def plans_due(self, now: datetime) -> Sequence[PlanAccount]:
        rows = self.db.all("""
            SELECT id::text AS user_id, plan::text AS plan, plan_period_end, plan_past_due_since, plan_started_at
              FROM users
             WHERE plan <> 'free' AND plan_period_end IS NOT NULL AND plan_period_end <= CAST(:n AS timestamptz)
             ORDER BY plan_period_end, id""", n=_ts(now))
        out = []
        for r in rows:
            try:
                price = self.economics.plan(r["plan"]).price_monthly_micro
            except KeyError:
                log.error("unknown_plan", extra={"fields": {"user_id": r["user_id"], "plan": r["plan"]}})
                continue
            out.append(PlanAccount(user_id=r["user_id"], plan=r["plan"], price_monthly_micro=int(price),
                                   plan_period_end=as_datetime(r["plan_period_end"]),
                                   past_due_since=as_datetime(r["plan_past_due_since"]),
                                   anchor=as_datetime(r["plan_started_at"])))
        return out

    def set_plan_period(self, user_id: str, plan_period_end: datetime | None, past_due_since: datetime | None) -> None:
        self.db.all("""UPDATE users SET plan_period_end = CAST(:e AS timestamptz),
                                        plan_past_due_since = CAST(:p AS timestamptz)
                        WHERE id = CAST(:u AS uuid)""", u=user_id, e=_ts(plan_period_end), p=_ts(past_due_since))

    def downgrade_plan(self, user_id: str, plan: str) -> None:
        self.db.all("UPDATE users SET plan = CAST(:p AS user_plan) WHERE id = CAST(:u AS uuid)", u=user_id, p=plan)


class PgReferralLookup:
    """``ReferralLookup``: the referrer and the share of the referral pool of their CURRENT tier
    (users.referral_tier, re-evaluated daily by jobs.referral_tiers)."""

    def __init__(self, db: PgDatabase, economics: Economics | None = None) -> None:
        self.db = db
        self.economics = economics or Economics()

    def referrer_share(self, user_id: str) -> tuple[str, int] | None:
        r = self.db.one("""SELECT r.id::text AS referrer_id, r.referral_tier::text AS tier
                             FROM users u JOIN users r ON r.id = u.referred_by
                            WHERE u.id = CAST(:u AS uuid)""", u=user_id)
        if not r:
            return None
        tiers = {t.name: t for t in self.economics.referral_tiers}
        tier = tiers.get(r["tier"]) or min(self.economics.referral_tiers, key=lambda t: t.share_of_pool_bps)
        return r["referrer_id"], int(tier.share_of_pool_bps)


class PgLedger:
    """``LedgerPoster`` / ``LedgerReader`` over ``app.ledger.service.BoundLedger`` on the shared ``PgDatabase``:
    inside ``atomic()`` the post joins the caller's transaction; outside it the post gets its own transaction."""

    def __init__(self, db: PgDatabase) -> None:
        from app.ledger.service import BoundLedger

        self.db = db
        self._bound = BoundLedger(db)

    def post_transaction(self, *, idempotency_key: str, kind: str, memo: str, lines: Sequence[Any],
                         created_by: str) -> tuple[str, bool]:
        with self.db.atomic():
            return self._bound.post_transaction(idempotency_key=idempotency_key, kind=kind, memo=memo, lines=lines,
                                                created_by=created_by)

    def has_transaction(self, idempotency_key: str) -> bool:
        return self._bound.has_transaction(idempotency_key)

    def balance(self, account_code: str) -> int:
        return self._bound.balance(account_code)


# ---------------------------------------------------------------------------------------------------------- reconcile

class PgReconcileRepo:
    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def expected_positions(self) -> Sequence[ExpectedPosition]:
        rows = self.db.all(f"""
            SELECT t.subscription_id::text AS subscription_id, s.user_id::text AS user_id, s.trading_address, t.coin,
                   t.target_notional_micro, s.allocation_micro, t.bar_close
              FROM subscription_targets t
              JOIN subscriptions s ON s.id = t.subscription_id
             WHERE s.status IN {_TRADABLE_SQL}
               AND EXISTS (SELECT 1 FROM subscription_bar_runs r
                            WHERE r.subscription_id = t.subscription_id AND r.bar_close = t.bar_close)
             ORDER BY s.trading_address, t.coin""")
        return [ExpectedPosition(subscription_id=r["subscription_id"], user_id=r["user_id"],
                                 trading_address=str(r["trading_address"]).lower(), coin=r["coin"],
                                 target_notional_micro=int(r["target_notional_micro"]),
                                 allocation_micro=int(r["allocation_micro"]), bar_close=as_datetime(r["bar_close"]))
                for r in rows]

    def total_builder_fees_micro(self) -> int:
        row = self.db.one("""SELECT coalesce(sum(builder_fee_micro), 0)::bigint AS total FROM fills
                              WHERE lower(coalesce(cloid, '')) LIKE CAST(:pfx AS text)""", pfx=_OUR_CLOID_LIKE)
        return int(row["total"]) if row else 0


class PgReconciliationStore:
    """reconciliation_reports (0005_exec.sql): one row per run; the admin console reads the latest."""

    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def save(self, date_key: str, report: Mapping[str, Any], created_at: datetime) -> None:
        self.db.all("""INSERT INTO reconciliation_reports (date_key, report, created_at)
                       VALUES (CAST(:d AS date), CAST(:r AS jsonb), CAST(:t AS timestamptz))""",
                    d=date_key, r=json_dumps(dict(report)), t=_ts(created_at))

    def latest(self) -> dict[str, Any] | None:
        r = self.db.one("""SELECT id::text AS id, date_key, report, created_at FROM reconciliation_reports
                            ORDER BY created_at DESC, id DESC LIMIT 1""")
        if not r:
            return None
        out = dict(_json(r["report"]) or {})
        out["date_key"] = str(as_date(r["date_key"]))
        out["created_at"] = as_datetime(r["created_at"]).isoformat()
        return out


# ---------------------------------------------------------------------------------------------------------- referrals

class PgReferralTierRepo:
    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def referrer_stats(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Per referrer (anyone with referred users, or a non-starter tier to demote): trailing-window active
        referred users (a live subscription now, or one cancelled inside the window) and referred notional
        (Σ |px × sz| of their attributed fills in the window, floored to the micro)."""
        return self.db.all("""
            WITH refs AS (
                SELECT r.id, r.referral_tier::text AS tier FROM users r
                 WHERE r.referral_tier <> 'starter' OR EXISTS (SELECT 1 FROM users u WHERE u.referred_by = r.id))
            SELECT refs.id::text AS referrer_id, refs.tier,
                   (SELECT count(DISTINCT u.id) FROM users u
                     WHERE u.referred_by = refs.id AND EXISTS (
                           SELECT 1 FROM subscriptions s WHERE s.user_id = u.id
                              AND (s.status IN ('active', 'past_due', 'reduce_only', 'closing')
                                   OR (s.cancelled_at IS NOT NULL AND s.cancelled_at >= CAST(:since AS timestamptz)))))
                       AS active_users,
                   (SELECT coalesce(sum(floor(f.px * f.sz * 1000000)), 0)::bigint
                      FROM fills f JOIN subscriptions s ON s.id = f.subscription_id JOIN users u ON u.id = s.user_id
                     WHERE u.referred_by = refs.id AND f.time >= CAST(:since AS timestamptz)
                       AND f.time < CAST(:until AS timestamptz)) AS notional_micro
              FROM refs ORDER BY refs.id""", since=_ts(since), until=_ts(until))

    def set_tier(self, user_id: str, tier: str) -> bool:
        rows = self.db.all("""UPDATE users SET referral_tier = CAST(:t AS referral_tier)
                               WHERE id = CAST(:u AS uuid) AND referral_tier <> CAST(:t AS referral_tier)
                              RETURNING id::text AS id""", u=user_id, t=tier)
        return bool(rows)


# ---------------------------------------------------------------------------------------------------------- creator signals

class PgCreatorSignalRepo:
    def __init__(self, db: PgDatabase) -> None:
        self.db = db

    def creator_versions(self) -> list[dict[str, Any]]:
        """Published creator versions to run: strategy listed/paused and either the latest published version or
        one that live subscriptions still trade. Includes the sealed code (app_executor may read it)."""
        rows = self.db.all(f"""
            SELECT v.id::text AS version_id, v.strategy_id::text AS strategy_id, v.version, v.code_hash,
                   v.code_ciphertext, coalesce(v.markets, st.markets) AS markets,
                   coalesce(v.timeframe, st.timeframe) AS timeframe, coalesce(v.lookback, 300) AS lookback,
                   coalesce(v.max_leverage, 1) AS max_leverage
              FROM strategy_versions v JOIN strategies st ON st.id = v.strategy_id
             WHERE st.in_house = false AND st.status IN ('listed', 'paused')
               AND v.published_at IS NOT NULL AND v.code_ciphertext IS NOT NULL
               AND (v.version = (SELECT max(v2.version) FROM strategy_versions v2
                                  WHERE v2.strategy_id = v.strategy_id AND v2.published_at IS NOT NULL)
                    OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.strategy_version_id = v.id
                                 AND s.status IN ('active', 'past_due', 'reduce_only')))
             ORDER BY v.strategy_id, v.version""")
        for r in rows:
            r["code_ciphertext"] = as_bytes(r["code_ciphertext"])
            r["markets"] = list(_markets(r["markets"]))
        return rows

    def has_signal(self, version_id: str, bar_close: datetime) -> bool:
        return self.db.one("""SELECT 1 AS x FROM signals WHERE strategy_version_id = CAST(:v AS uuid)
                               AND bar_close = CAST(:b AS timestamptz) LIMIT 1""",
                           v=version_id, b=_ts(bar_close)) is not None

    def insert_signals(self, *, strategy_id: str, version_id: str, bar_close: datetime,
                       weights_bps: Mapping[str, int], raw: Mapping[str, Any]) -> int:
        n = 0
        with self.db.atomic():
            for coin in sorted(weights_bps):
                rows = self.db.all("""
                    INSERT INTO signals (strategy_id, strategy_version_id, bar_close, coin, target_weight_bps, source, raw)
                    VALUES (CAST(:s AS uuid), CAST(:v AS uuid), CAST(:b AS timestamptz), CAST(:c AS text),
                            CAST(:w AS integer), CAST('sandbox' AS signal_source), CAST(:r AS jsonb))
                    ON CONFLICT (strategy_version_id, bar_close, coin) DO NOTHING
                    RETURNING id::text AS id""",
                    s=strategy_id, v=version_id, b=_ts(bar_close), c=coin, w=int(weights_bps[coin]), r=json_dumps(raw))
                n += len(rows)
        return n

    def stored_candles(self, coin: str, interval: str, limit: int) -> list[dict[str, Any]]:
        """Newest ``limit`` stored candles (table ``candles``, SPEC §12; created by the data-jobs migration).
        Prices come back as text (NUMERIC strings); open_time as timestamptz or ms, whichever the table uses."""
        return self.db.all("""
            SELECT open_time, o::text AS o, h::text AS h, l::text AS l, c::text AS c, v::text AS v
              FROM candles WHERE coin = CAST(:c AS text) AND interval = CAST(:i AS text)
             ORDER BY open_time DESC LIMIT CAST(:n AS integer)""", c=coin, i=interval, n=int(limit))


def open_time_ms(v: Any) -> int:
    """candles.open_time as ms whatever its column type (timestamptz / bigint ms / numeric)."""
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    dt = as_datetime(v)
    return int(dt.timestamp() * 1000)
