"""Postgres implementation of ``app.ledger.service.LedgerStore``.

Writes go through the SQL function ``ledger_post_as(role, …)`` (0015, SECURITY DEFINER — the app roles have no
INSERT on the ledger tables any more). ``role`` is the app role the caller posts AS (``app_api`` / ``app_executor``);
the DB checks that the session (SET ROLE, else the login) is a member of it and that the posting matches a row of the
fixed ``ledger_posting_rules`` table for (role, kind) — key pattern, debit / credit account patterns, entry counts —
then validates, locks the ledger chain, handles idempotency race-free, checks balances and records the authorisation
(rule id, role, login); the deferred constraint triggers re-check at COMMIT. ``PostgresLedgerStore(conn, role=None)``
lets the DB pick the invoker's single app role (``ledger_invoker_app_role()``; owner sessions post as app_migrator).
Custom SQLSTATEs are mapped to ``app.errors`` (a posting no rule allows → AJ403 → ValidationFailed).
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any, Mapping, Sequence

from app.db.engine import SqlAlchemyRunner, SqlRunner, sqlstate_of
from app.errors import AppError, Conflict, InsufficientBalance, NotFound, ValidationFailed
from app.ledger.service import Account, PostedTx

__all__ = ["PostgresLedgerStore", "map_db_error", "POSTING_ROLES"]

_SQLSTATE_TO_ERROR: dict[str, type[AppError]] = {
    "AJ402": InsufficientBalance,
    "AJ403": ValidationFailed,   # append-only violation: a programming error, never user input
    "AJ404": NotFound,
    "AJ409": Conflict,
    "AJ422": ValidationFailed,
    "23505": Conflict,           # unique violation (e.g. idempotency_key race without the lock)
    "23503": NotFound,           # FK violation (e.g. owner user does not exist)
    "23514": ValidationFailed,   # CHECK violation
}


def map_db_error(exc: BaseException) -> BaseException:
    state = sqlstate_of(exc)
    cls = _SQLSTATE_TO_ERROR.get(state or "")
    if cls is None:
        return exc
    msg = getattr(exc, "message", None) or str(exc)
    first_line = msg.strip().splitlines()[0] if msg.strip() else cls.code
    return cls(first_line, sqlstate=state)


_ACCOUNT_SQL = """
SELECT id::text AS id, code, kind::text AS kind, owner_user_id::text AS owner_user_id, non_negative
  FROM ledger_accounts WHERE code = :code
"""

_INSERT_ACCOUNT_SQL = """
INSERT INTO ledger_accounts (code, kind, owner_user_id, non_negative)
VALUES (:code, CAST(:kind AS ledger_account_kind), CAST(:owner AS uuid), :non_negative)
ON CONFLICT (code) DO NOTHING
"""

_TX_SQL = """
SELECT t.id::text AS id, t.idempotency_key, t.kind, t.memo, t.created_by, t.seq,
       utc_iso(t.created_at) AS created_at, t.entries_digest, t.prev_hash, t.hash,
       (SELECT coalesce(json_agg(json_build_array(a.code, e.amount_micro)
                                 ORDER BY a.code COLLATE "C", e.amount_micro), CAST('[]' AS json))
          FROM ledger_entries e JOIN ledger_accounts a ON a.id = e.account_id
         WHERE e.tx_id = t.id) AS entries
  FROM ledger_transactions t
 WHERE t.idempotency_key = :key
"""

# 0015: the one posting entry point for the app roles. A NULL role → the DB resolves the invoker's app role.
_POST_SQL = """
SELECT tx_id::text AS tx_id, created
  FROM ledger_post_as(coalesce(CAST(:role AS text), ledger_invoker_app_role()), :key, :kind, :memo, :created_by,
                      CAST(:entries AS jsonb))
"""

POSTING_ROLES = ("app_api", "app_executor", "app_migrator")

_BALANCE_SQL = """
SELECT (SELECT coalesce(sum(e.amount_micro), 0) FROM ledger_entries e WHERE e.account_id = a.id)::bigint AS balance
  FROM ledger_accounts a WHERE a.code = :code
"""

_HISTORY_SQL = """
SELECT t.id::text AS tx_id, t.seq, utc_iso(t.created_at) AS created_at, t.kind, t.memo, e.amount_micro
  FROM ledger_entries e
  JOIN ledger_accounts a ON a.id = e.account_id
  JOIN ledger_transactions t ON t.id = e.tx_id
 WHERE a.code = :code AND (CAST(:before_seq AS bigint) IS NULL OR t.seq < CAST(:before_seq AS bigint))
 ORDER BY t.seq DESC
 LIMIT :limit
"""


class PostgresLedgerStore:
    def __init__(self, conn: Any, *, role: str | None = None) -> None:
        # a SqlRunner (has fetchall) or a SQLAlchemy Connection
        self.runner: SqlRunner = conn if hasattr(conn, "fetchall") else SqlAlchemyRunner(conn)
        if role is not None and role not in POSTING_ROLES:
            raise ValidationFailed("unknown ledger posting role", role=role)
        self.role = role              # None: the DB uses the session's own app role

    def _q(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return self.runner.fetchall(sql, params)
        except Exception as e:
            mapped = map_db_error(e)
            if mapped is e:
                raise
            raise mapped from e

    # -- accounts
    def get_account(self, code: str) -> Account | None:
        rows = self._q(_ACCOUNT_SQL, {"code": code})
        if not rows:
            return None
        r = rows[0]
        return Account(str(r["id"]), r["code"], r["kind"], r["owner_user_id"], bool(r["non_negative"]))

    def insert_account(self, code: str, kind: str, owner_user_id: str | None, non_negative: bool) -> Account:
        self._q(_INSERT_ACCOUNT_SQL, {"code": code, "kind": kind, "owner": owner_user_id,
                                      "non_negative": bool(non_negative)})
        acct = self.get_account(code)
        if acct is None:  # pragma: no cover - impossible after the insert
            raise NotFound("ledger account not created", code=code)
        return acct

    # -- transactions
    def get_tx_by_key(self, idempotency_key: str) -> PostedTx | None:
        rows = self._q(_TX_SQL, {"key": idempotency_key})
        if not rows:
            return None
        r = rows[0]
        entries = r["entries"]
        if isinstance(entries, str):
            entries = json.loads(entries)
        return PostedTx(
            id=str(r["id"]), idempotency_key=r["idempotency_key"], kind=r["kind"], memo=r["memo"],
            created_by=r["created_by"], entries=tuple(sorted((str(c), int(a)) for c, a in entries)),
            seq=int(r["seq"]), created_at=r["created_at"], entries_digest=r["entries_digest"],
            prev_hash=r["prev_hash"], hash=r["hash"], created=False,
        )

    def insert_tx(self, idempotency_key: str, kind: str, memo: str | None, created_by: str,
                  entries: Sequence[tuple[str, int]]) -> PostedTx:
        payload = json.dumps([{"account": c, "amount_micro": int(a)} for c, a in entries])
        savepoint = getattr(self.runner, "savepoint", None)
        with (savepoint() if savepoint else nullcontext()):
            rows = self._q(_POST_SQL, {"role": self.role, "key": idempotency_key, "kind": kind, "memo": memo,
                                       "created_by": created_by, "entries": payload})
        created = bool(rows[0]["created"]) if rows else False
        tx = self.get_tx_by_key(idempotency_key)
        if tx is None:  # pragma: no cover
            raise NotFound("ledger transaction vanished", idempotency_key=idempotency_key)
        from dataclasses import replace

        return replace(tx, created=created)

    # -- balances
    def balance(self, code: str) -> int | None:
        rows = self._q(_BALANCE_SQL, {"code": code})
        return int(rows[0]["balance"]) if rows else None

    def history(self, code: str, *, limit: int = 50, before_seq: int | None = None) -> list[dict[str, Any]]:
        """Entries of one account, newest first (for GET /balance history). Keyset pagination on seq."""
        return self._q(_HISTORY_SQL, {"code": code, "limit": max(1, min(int(limit), 500)), "before_seq": before_seq})
