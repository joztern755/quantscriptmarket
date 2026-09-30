"""In-memory ``LedgerStore`` for tests and dev. Same rules as the Postgres ledger (0001 + 0010): balanced,
>= 2 non-zero entries, idempotency by (kind, entries multiset), non-negative accounts cannot be decreased below 0
except by the fixed (kind, account) allowlist ``overdraft_allowed`` (fee balances only), protected accounts are forced
non-negative, ps_pending accounts only move by ps_pending_release, append-only, and the same hash chain (so
hashes are comparable with the DB's). Role gating of kinds is a DB-only control (no roles here)."""
from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Sequence

from app.errors import Conflict, InsufficientBalance, NotFound, ValidationFailed
from app.ledger.service import (
    DEBIT_NORMAL,
    GENESIS_HASH,
    PS_PENDING_RELEASE_KIND,
    Account,
    PostedTx,
    entries_digest,
    forced_non_negative,
    normalize_entries,
    overdraft_allowed,
    tx_hash,
)

__all__ = ["InMemoryLedgerStore"]


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class InMemoryLedgerStore:
    def __init__(self, *, clock: Callable[[], datetime] | None = None, seed_platform_accounts: bool = True) -> None:
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.accounts: dict[str, Account] = {}
        self.txs: list[PostedTx] = []
        self._by_key: dict[str, PostedTx] = {}
        self._balances: dict[str, int] = {}
        if seed_platform_accounts:
            for code, kind in (("platform:revenue:builder", "revenue"), ("platform:revenue:profit_share", "revenue"),
                               ("platform:revenue:subscription", "revenue"), ("platform:revenue:posts", "revenue"),
                               ("platform:revenue:plans", "revenue"), ("treasury:hl_usdc", "asset"),
                               ("stripe:clearing", "asset"), ("builder:hl_receivable", "asset")):
                self.insert_account(code, kind, None, False)

    # -- accounts
    def get_account(self, code: str) -> Account | None:
        return self.accounts.get(code)

    def insert_account(self, code: str, kind: str, owner_user_id: str | None, non_negative: bool) -> Account:
        with self._lock:
            if code not in self.accounts:
                if code.startswith("user:") and code.endswith(":fee_balance") and not (
                        kind == "liability" and non_negative and owner_user_id):
                    raise ValidationFailed("fee balance accounts must be non-negative liabilities with an owner")
                non_negative = bool(non_negative) or forced_non_negative(code)
                self.accounts[code] = Account(str(uuid.uuid4()), code, kind, owner_user_id, bool(non_negative))
                self._balances[code] = 0
            return self.accounts[code]

    # -- transactions
    def get_tx_by_key(self, idempotency_key: str) -> PostedTx | None:
        tx = self._by_key.get(idempotency_key)
        return replace(tx, created=False) if tx else None

    def insert_tx(self, idempotency_key: str, kind: str, memo: str | None, created_by: str,
                  entries: Sequence[tuple[str, int]]) -> PostedTx:
        canon = normalize_entries(entries)
        digest = entries_digest(canon)
        with self._lock:
            existing = self._by_key.get(idempotency_key)
            if existing is not None:
                if existing.kind == kind and existing.entries_digest == digest:
                    return replace(existing, created=False)
                raise Conflict("idempotency key already used with different content", idempotency_key=idempotency_key)
            missing = sorted({c for c, _ in canon if c not in self.accounts})
            if missing:
                raise NotFound("unknown ledger account(s)", codes=missing)
            deltas: dict[str, int] = {}
            for c, a in canon:
                deltas[c] = deltas.get(c, 0) + a
            if kind != PS_PENDING_RELEASE_KIND and any(c.startswith("ps_pending:") and raw > 0 for c, raw in canon):
                raise ValidationFailed("ps_pending accounts may only be debited by ps_pending_release")
            for c, raw in deltas.items():
                acct = self.accounts[c]
                if not acct.non_negative or overdraft_allowed(kind, c):
                    continue
                sign = 1 if acct.kind in DEBIT_NORMAL else -1
                after = sign * (self._balances[c] + raw)
                if sign * raw < 0 and after < 0:
                    raise InsufficientBalance("insufficient balance", account=c, shortfall_micro=-after)
            prev = self.txs[-1].hash if self.txs else GENESIS_HASH
            tx_id = str(uuid.uuid4())
            created_at = _utc_iso(self._clock())
            h = tx_hash(prev, id=tx_id, idempotency_key=idempotency_key, kind=kind, memo=memo, created_by=created_by,
                        created_at=created_at, entries_digest=digest)
            tx = PostedTx(id=tx_id, idempotency_key=idempotency_key, kind=kind, memo=memo, created_by=created_by,
                          entries=canon, seq=len(self.txs) + 1, created_at=created_at, entries_digest=digest,
                          prev_hash=prev, hash=h, created=True)
            for c, raw in deltas.items():
                self._balances[c] += raw
            self.txs.append(tx)
            self._by_key[idempotency_key] = tx
            return tx

    # -- balances
    def balance(self, code: str) -> int | None:
        return self._balances.get(code) if code in self.accounts else None

    def verify_chain(self) -> int | None:
        """Seq of the first broken transaction, or None when intact."""
        prev = GENESIS_HASH
        for tx in self.txs:
            expect = tx_hash(prev, id=tx.id, idempotency_key=tx.idempotency_key, kind=tx.kind, memo=tx.memo,
                             created_by=tx.created_by, created_at=tx.created_at,
                             entries_digest=entries_digest(tx.entries))
            if tx.prev_hash != prev or tx.hash != expect:
                return tx.seq
            prev = tx.hash
        return None
