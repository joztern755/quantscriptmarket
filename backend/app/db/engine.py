"""Database access foundation: SQLAlchemy Core engine factory + a tiny SQL-runner protocol.

Repositories talk to a ``SqlRunner`` (one method: ``fetchall(sql, params) -> list[dict]``) instead of a driver,
so they can be exercised with a fake, with ``SqlAlchemyRunner`` in prod, or with the psql-backed runner used by
the local DB tests. SQL uses SQLAlchemy ``text()`` bind style (``:name``); never ``::type`` casts next to binds —
write ``CAST(:x AS jsonb)``.

SQLAlchemy / psycopg are imported lazily (import-guarded) so stdlib-only code and tests can import this module.

Transactions: the caller owns them (``with transaction(engine) as conn: ...``); ledger writes then commit
atomically with the business rows they belong to. Use READ COMMITTED (the default) for ledger writes.
"""
from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext
from typing import Any, ContextManager, Iterator, Mapping, Protocol

__all__ = [
    "SqlRunner", "DbError", "sqlstate_of", "sqlalchemy_url", "libpq_url", "create_db_engine", "transaction",
    "SqlAlchemyRunner",
]


class SqlRunner(Protocol):
    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute one statement; return rows as dicts ([] for statements without rows).
        Raise ``DbError`` (with ``sqlstate``) for database errors."""


class DbError(Exception):
    """A database error with its SQLSTATE (e.g. '23505', or our custom 'AJ402')."""

    def __init__(self, sqlstate: str | None, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.message = message


def sqlstate_of(exc: BaseException) -> str | None:
    """SQLSTATE from a psycopg 3 / psycopg2 / SQLAlchemy-wrapped exception (or our DbError)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("sqlstate", "pgcode"):
            v = getattr(cur, attr, None)
            if isinstance(v, str) and len(v) == 5:
                return v
        diag = getattr(cur, "diag", None)
        v = getattr(diag, "sqlstate", None) if diag is not None else None
        if isinstance(v, str) and len(v) == 5:
            return v
        cur = getattr(cur, "orig", None) or cur.__cause__
    return None


def libpq_url(url: str) -> str:
    """'postgresql+psycopg://…' -> 'postgresql://…' (for psql / psycopg.connect)."""
    return re.sub(r"^postgres(ql)?\+[a-z0-9_]+://", "postgresql://", url)


def sqlalchemy_url(url: str) -> str:
    """'postgresql://…' / 'postgres://…' -> 'postgresql+psycopg://…' (psycopg 3 driver)."""
    if re.match(r"^postgres(ql)?\+[a-z0-9_]+://", url):
        return url
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)


def create_db_engine(settings: Any = None, *, application_name: str = "aijalon", pool_size: int = 5,
                     max_overflow: int = 5, statement_timeout_ms: int = 30_000, **engine_kwargs: Any) -> Any:
    """Build a SQLAlchemy Core Engine from ``settings.database_url`` (default: ``app.config.get_settings()``).
    Sessions run in UTC with a statement timeout; connections are health-checked and recycled."""
    try:
        import sqlalchemy  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover - prod always has it
        raise RuntimeError("SQLAlchemy is not installed; install backend/requirements.txt") from e
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    connect_args = {
        "application_name": application_name,
        "options": f"-c timezone=UTC -c statement_timeout={int(statement_timeout_ms)}",
    }
    connect_args.update(engine_kwargs.pop("connect_args", {}))
    return sqlalchemy.create_engine(
        sqlalchemy_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=1800,
        connect_args=connect_args,
        **engine_kwargs,
    )


@contextmanager
def transaction(engine: Any) -> Iterator[Any]:
    """``with transaction(engine) as conn:`` — commit on success, roll back on any exception."""
    with engine.begin() as conn:
        yield conn


class SqlAlchemyRunner:
    """``SqlRunner`` over a SQLAlchemy ``Connection`` (inside the caller's transaction)."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        from sqlalchemy import text  # type: ignore[import-not-found]

        try:
            result = self.conn.execute(text(sql), dict(params or {}))
        except Exception as e:
            state = sqlstate_of(e)
            if state is not None:
                raise DbError(state, str(getattr(e, "orig", e))) from e
            raise
        if not result.returns_rows:
            return []
        return [dict(r) for r in result.mappings().all()]

    def savepoint(self) -> ContextManager[Any]:
        """Nested transaction so a failed ledger post does not poison the caller's transaction."""
        begin_nested = getattr(self.conn, "begin_nested", None)
        return begin_nested() if begin_nested else nullcontext()
