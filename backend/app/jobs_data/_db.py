"""SQL helpers for the data jobs (same conventions as app/api/store.py and app/alerts/_db.py).

``db`` handed to a job is the API's ``DatabasePort`` (``db.begin()`` → SQLAlchemy Connection in one transaction) or,
in tests, anything with ``begin()``; a bare ``conn`` is either a SQLAlchemy Connection or a ``SqlRunner`` (has
``fetchall``; e.g. the tests' psql runner). Every value is a bound parameter; SQL uses ``CAST(:x AS type)`` (never
``::type`` next to a bind). Values are read back in driver-neutral forms: ids as ``::text``, numerics as ``::text``,
timestamps as epoch milliseconds (``_ms`` columns), so psycopg and the psql test runner return the same thing.

Units of work are small (one series / one address / one deposit per transaction) and the network is never called
inside a transaction.
"""
from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Optional

from app.logging import get_logger

log = get_logger("app.jobs_data")

#: SQL expression turning a bigint ms column/bind into timestamptz exactly (no float rounding).
MS_TO_TS = "(timestamptz 'epoch' + {x} * interval '1 millisecond')"
#: SQL expression turning a timestamptz into epoch ms (bigint).
TS_TO_MS = "(floor(extract(epoch FROM {x}) * 1000))::bigint"


def ms_to_ts(expr: str) -> str:
    return MS_TO_TS.format(x=expr)


def ts_to_ms(expr: str) -> str:
    return TS_TO_MS.format(x=expr)


def runner(conn: Any) -> Any:
    if hasattr(conn, "fetchall"):
        return conn
    from app.db.engine import SqlAlchemyRunner  # raises DbError(sqlstate) on database errors

    return SqlAlchemyRunner(conn)


def rows(conn: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in runner(conn).fetchall(sql, params)]


def one(conn: Any, sql: str, **params: Any) -> Optional[dict[str, Any]]:
    r = rows(conn, sql, **params)
    return r[0] if r else None


def jdump(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)


def jload(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        return v
    if isinstance(v, (str, bytes)):
        try:
            out = json.loads(v)
        except ValueError:
            return {}
        return out if isinstance(out, dict) else {}
    return {}


def now_ms(now: datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware UTC")
    return int(now.timestamp() * 1000)


def dt_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@contextmanager
def transaction(db: Any) -> Iterator[Any]:
    """``with transaction(db) as conn`` — ``db.begin()`` when available, else ``db`` itself is the connection."""
    begin = getattr(db, "begin", None)
    if callable(begin) and not hasattr(db, "fetchall"):
        with begin() as conn:
            yield conn
    else:
        with nullcontext(db) as conn:
            yield conn


# ------------------------------------------------------------------------------------------------ cursors
def get_cursor(conn: Any, job: str, key: str) -> tuple[Optional[int], dict[str, Any]]:
    r = one(conn, "SELECT cursor_ms, state FROM job_cursors WHERE job = :j AND key = :k", j=job, k=key)
    if r is None:
        return None, {}
    c = r.get("cursor_ms")
    return (int(c) if c is not None else None), jload(r.get("state"))


def all_cursors(conn: Any, job: str) -> dict[str, tuple[Optional[int], dict[str, Any]]]:
    out: dict[str, tuple[Optional[int], dict[str, Any]]] = {}
    for r in rows(conn, "SELECT key, cursor_ms, state FROM job_cursors WHERE job = :j", j=job):
        c = r.get("cursor_ms")
        out[str(r["key"])] = ((int(c) if c is not None else None), jload(r.get("state")))
    return out


def set_cursor(conn: Any, job: str, key: str, cursor_ms: Optional[int], state: Mapping[str, Any] | None = None,
               *, monotonic: bool = True) -> None:
    """Upsert a cursor. ``monotonic``: never move an existing cursor backwards (concurrent runs are harmless)."""
    rows(conn, """
        INSERT INTO job_cursors (job, key, cursor_ms, state) VALUES (:j, :k, :c, CAST(:s AS jsonb))
        ON CONFLICT (job, key) DO UPDATE
           SET cursor_ms = CASE WHEN :mono AND job_cursors.cursor_ms IS NOT NULL
                                     AND (EXCLUDED.cursor_ms IS NULL OR EXCLUDED.cursor_ms < job_cursors.cursor_ms)
                                THEN job_cursors.cursor_ms ELSE EXCLUDED.cursor_ms END,
               state = EXCLUDED.state
        RETURNING job""", j=job, k=key, c=cursor_ms, s=jdump(dict(state or {})), mono=bool(monotonic))


# ------------------------------------------------------------------------------------------------ events
def emit_event(conn: Any, *, kind: str, payload: Mapping[str, Any], user_id: Optional[str] = None,
               severity: str = "info", dedup_key: Optional[str] = None) -> bool:
    """Insert one ``events_outbox`` row (the alerts module delivers it). Returns False when ``dedup_key`` was
    already used (the event exists). ``user_id`` None = ops event. Payload must not carry full addresses/keys."""
    r = one(conn, """
        INSERT INTO events_outbox (user_id, kind, severity, payload, dedup_key)
        VALUES (CAST(:u AS uuid), :k, CAST(:sev AS alert_severity), CAST(:p AS jsonb), :d)
        ON CONFLICT (dedup_key) DO NOTHING
        RETURNING id""", u=user_id, k=kind, sev=severity, p=jdump(dict(payload)), d=dedup_key[:200] if dedup_key else None)
    if r is not None:
        log.info("event_emitted", extra={"fields": {"kind": kind, "severity": severity, "ops": user_id is None}})
    return r is not None


def ops_alert(conn: Any, kind: str, payload: Mapping[str, Any], *, severity: str = "warn",
              dedup_key: Optional[str] = None) -> bool:
    """Ops (user_id NULL) event; the alerts module materialises it into ``alerts`` and pages ops."""
    return emit_event(conn, kind=kind, payload=payload, user_id=None, severity=severity, dedup_key=dedup_key)


def pause_market_entries(conn: Any, coin: str, reason: str, *, actor: str = "system:jobs_data") -> bool:
    """SPEC §5.5 auto-pause: set ``new_entries_paused:{coin}`` = true (reduce-only on that market). Never lifts a
    pause (lifting is maker-checker in the admin flags flow). Returns True when the flag changed."""
    r = one(conn, """
        INSERT INTO system_flags (key, value, updated_by) VALUES (:k, CAST('true' AS jsonb), :by)
        ON CONFLICT (key) DO UPDATE SET value = CAST('true' AS jsonb), updated_by = EXCLUDED.updated_by
         WHERE system_flags.value <> CAST('true' AS jsonb)
        RETURNING key""", k=f"new_entries_paused:{coin}", by=f"{actor}:{reason}"[:200])
    return r is not None


def short_addr(addr: Optional[str]) -> str:
    s = str(addr or "")
    return s[:6] + "…" + s[-4:] if len(s) >= 12 else s
