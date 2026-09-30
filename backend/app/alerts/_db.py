"""Tiny SQL helpers shared by the user-alert modules (same conventions as app/api/store.py).

``conn`` is either a SqlRunner (has ``fetchall``; e.g. the tests' psql runner) or a SQLAlchemy Connection inside
an open transaction. Every value is a bound parameter; timestamps read back may be ``datetime`` (psycopg) or ISO
strings (psql runner) — use :func:`ts` when Python needs one.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Optional


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
    return json.dumps(obj, separators=(",", ":"), default=str)


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


def ts(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v).strip().replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # psql prints "+00" offsets; fromisoformat wants "+00:00"
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():
        s += ":00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def savepoint(conn: Any) -> Any:
    """Nested transaction when the connection supports it (SQLAlchemy); a no-op for autocommit runners."""
    begin_nested = getattr(conn, "begin_nested", None)
    if begin_nested is not None:
        return begin_nested()
    sp = getattr(conn, "savepoint", None)
    return sp() if sp is not None else nullcontext()
