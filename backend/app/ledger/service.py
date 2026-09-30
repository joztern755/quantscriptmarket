"""Double-entry ledger service (SPEC §4, §5.6). Append-only, hash-chained, idempotent.

SIGN CONVENTION (same as backend/migrations/0001_init.sql header):
  * ``amount_micro`` > 0 = DEBIT, < 0 = CREDIT; every transaction has >= 2 non-zero entries summing to 0.
  * ``get_balance`` returns the RAW signed balance Σ amount_micro. Asset/expense accounts are normally >= 0;
    liability/revenue accounts are normally <= 0 (credit = what we owe / earned).
  * A user's spendable fee balance = −get_balance("user:{id}:fee_balance") = ``available_fee_balance``.
  * Non-negative accounts (every user fee balance; creator/referrer payables created here) may not be
    DECREASED below zero, except by kinds in ``OVERDRAFT_KINDS`` (debts that exist regardless). The DB enforces
    the same rule at commit (SQLSTATE AJ402 -> ``InsufficientBalance``).

Idempotency: ``idempotency_key`` is unique. Re-posting the same key with the same kind and the same entries
(as a multiset; memo/created_by are not compared) returns the existing transaction with ``created=False``;
different content raises ``Conflict``.

Storage is behind the small ``LedgerStore`` protocol:
  * ``app.db.repositories.ledger.PostgresLedgerStore`` (prod; calls SQL function ``ledger_post``)
  * ``app.ledger.memory.InMemoryLedgerStore`` (tests; same rules, same hash chain)
Every public function takes ``conn`` = a ``LedgerStore`` or a SQLAlchemy Connection / ``SqlRunner`` (wrapped in
``PostgresLedgerStore`` automatically).

Adapters for the other modules' ports:
  * ``LedgerService``  -> app.api.deps.LedgerPort   (``post(conn, …) -> tx_id``, ``balance(conn, code)``)
  * ``BoundLedger``    -> app.execution.ports.LedgerPoster / LedgerReader
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from app.errors import Conflict, InsufficientBalance, NotFound, ValidationFailed

__all__ = [
    "Account", "PostedTx", "LedgerStore", "ACCOUNT_KINDS", "OVERDRAFT_KINDS", "GENESIS_HASH",
    "PLATFORM_REVENUE_BUILDER", "PLATFORM_REVENUE_PROFIT_SHARE", "PLATFORM_REVENUE_SUBSCRIPTION",
    "PLATFORM_REVENUE_POSTS", "PLATFORM_REVENUE_PLANS", "TREASURY_HL_USDC", "STRIPE_CLEARING", "BUILDER_HL_RECEIVABLE",
    "fee_balance_account", "creator_payable_account", "referrer_payable_account", "default_account_spec",
    "validate_account_code", "normalize_entries", "entries_digest", "canonical_json", "tx_hash",
    "ensure_account", "post_transaction", "get_balance", "normal_balance", "available_fee_balance",
    "get_transaction", "LedgerService", "BoundLedger",
]

ACCOUNT_KINDS = ("asset", "liability", "revenue", "expense")
DEBIT_NORMAL = frozenset({"asset", "expense"})
# Must equal SQL ledger_kind_allows_overdraft() in 0001_init.sql.
OVERDRAFT_KINDS = frozenset({"profit_share", "stripe_refund", "stripe_dispute"})
GENESIS_HASH = "0" * 64
MAX_ABS_MICRO = 10**18 - 1          # fits bigint; also the SQL input check (<= 18 digits)

# Seeded by 0003_seed.sql
PLATFORM_REVENUE_BUILDER = "platform:revenue:builder"
PLATFORM_REVENUE_PROFIT_SHARE = "platform:revenue:profit_share"
PLATFORM_REVENUE_SUBSCRIPTION = "platform:revenue:subscription"
PLATFORM_REVENUE_POSTS = "platform:revenue:posts"
PLATFORM_REVENUE_PLANS = "platform:revenue:plans"
TREASURY_HL_USDC = "treasury:hl_usdc"
STRIPE_CLEARING = "stripe:clearing"
BUILDER_HL_RECEIVABLE = "builder:hl_receivable"

_CODE_RE = re.compile(r"^[a-z0-9_]+(:[a-z0-9_.-]+)+$")
_TX_KIND_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# (pattern, kind, non_negative) for accounts created on demand; group 1 = owner user id
_PER_USER_ACCOUNTS = (
    (re.compile(rf"^user:({_UUID}):fee_balance$"), "liability", True),
    (re.compile(rf"^creator:({_UUID}):payable$"), "liability", True),
    (re.compile(rf"^referrer:({_UUID}):payable$"), "liability", True),
)


def fee_balance_account(user_id: str) -> str:
    return f"user:{str(user_id).lower()}:fee_balance"


def creator_payable_account(user_id: str) -> str:
    return f"creator:{str(user_id).lower()}:payable"


def referrer_payable_account(user_id: str) -> str:
    return f"referrer:{str(user_id).lower()}:payable"


# ------------------------------------------------------------------------------------------------ value types

@dataclass(frozen=True)
class Account:
    id: str
    code: str
    kind: str
    owner_user_id: str | None
    non_negative: bool


@dataclass(frozen=True)
class PostedTx:
    id: str
    idempotency_key: str
    kind: str
    memo: str | None
    created_by: str
    entries: tuple[tuple[str, int], ...]   # canonical order (code, amount) sorted
    seq: int
    created_at: str                        # UTC ISO-8601 "+00:00", microseconds (hashed form)
    entries_digest: str
    prev_hash: str
    hash: str
    created: bool = True                   # False when an idempotent replay returned an existing tx


@runtime_checkable
class LedgerStore(Protocol):
    def get_account(self, code: str) -> Account | None: ...

    def insert_account(self, code: str, kind: str, owner_user_id: str | None, non_negative: bool) -> Account:
        """Create if missing (no error if it exists); return the stored row."""

    def get_tx_by_key(self, idempotency_key: str) -> PostedTx | None: ...

    def insert_tx(self, idempotency_key: str, kind: str, memo: str | None, created_by: str,
                  entries: Sequence[tuple[str, int]]) -> PostedTx:
        """Atomically post (or idempotently return) a transaction. Raises Conflict / InsufficientBalance /
        NotFound (unknown account) / ValidationFailed like the DB does."""

    def balance(self, code: str) -> int | None:
        """Raw Σ amount_micro, or None when the account does not exist."""


# ------------------------------------------------------------------------------------------------ pure helpers

def validate_account_code(code: str) -> str:
    if not isinstance(code, str) or not _CODE_RE.match(code):
        raise ValidationFailed("invalid ledger account code", code=code)
    return code


def default_account_spec(code: str) -> tuple[str, str | None, bool] | None:
    """(kind, owner_user_id, non_negative) for per-user accounts that may be created on demand, else None."""
    for pat, kind, non_neg in _PER_USER_ACCOUNTS:
        m = pat.match(code)
        if m:
            return kind, m.group(1), non_neg
    return None


def normalize_entries(entries: Iterable[Any]) -> tuple[tuple[str, int], ...]:
    """Accept (code, amount) pairs or objects with ``account_code``/``amount_micro``; validate; return them in
    canonical order. Raises ValidationFailed for unbalanced / malformed input."""
    out: list[tuple[str, int]] = []
    for e in entries:
        if isinstance(e, (tuple, list)) and len(e) == 2:
            code, amt = e
        else:
            code, amt = getattr(e, "account_code", None), getattr(e, "amount_micro", None)
        validate_account_code(code)  # type: ignore[arg-type]
        if not isinstance(amt, int) or isinstance(amt, bool):
            raise ValidationFailed("ledger amounts must be int micro-USD", account=code)
        if amt == 0:
            raise ValidationFailed("zero-amount ledger entry", account=code)
        if abs(amt) > MAX_ABS_MICRO:
            raise ValidationFailed("ledger amount out of range", account=code)
        out.append((code, amt))  # type: ignore[arg-type]
    if len(out) < 2:
        raise ValidationFailed("a ledger transaction needs at least 2 entries")
    if sum(a for _, a in out) != 0:
        raise ValidationFailed("unbalanced ledger transaction", sum_micro=sum(a for _, a in out))
    return tuple(sorted(out))


def canonical_json(obj: Any) -> str:
    """= SQL canonical_json(jsonb) = app.security.audit.canonical_json (as text)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def entries_digest(entries: Iterable[tuple[str, int]]) -> str:
    """= SQL ledger_entries_digest(codes, amounts): order-independent multiset digest."""
    canon = sorted([c, int(a)] for c, a in entries)
    return hashlib.sha256(canonical_json(canon).encode("utf-8")).hexdigest()


def tx_hash(prev_hash: str, *, id: str, idempotency_key: str, kind: str, memo: str | None, created_by: str,
            created_at: str, entries_digest: str) -> str:
    """= SQL ledger_tx_hash(): sha256(prev_hash || "\\n" || canonical_json(body)), hex."""
    body = {"id": id, "idempotency_key": idempotency_key, "kind": kind, "memo": memo, "created_by": created_by,
            "created_at": created_at, "entries_digest": entries_digest}
    return hashlib.sha256((prev_hash + "\n" + canonical_json(body)).encode("utf-8")).hexdigest()


def _as_store(conn: Any) -> LedgerStore:
    if isinstance(conn, LedgerStore):
        return conn
    from app.db.repositories.ledger import PostgresLedgerStore  # lazy: keeps this module DB-free

    return PostgresLedgerStore(conn)


# ------------------------------------------------------------------------------------------------ operations

def ensure_account(conn: Any, code: str, kind: str, owner_user_id: str | None = None, *,
                   non_negative: bool | None = None) -> Account:
    """Get-or-create a ledger account. Raises Conflict if it exists with a different kind/owner."""
    validate_account_code(code)
    if kind not in ACCOUNT_KINDS:
        raise ValidationFailed("invalid ledger account kind", kind=kind)
    spec = default_account_spec(code)
    if non_negative is None:
        non_negative = spec[2] if spec else False
    if spec and spec[2] and not non_negative:
        raise ValidationFailed("per-user fee/payable accounts are always non-negative", code=code)
    owner = str(owner_user_id).lower() if owner_user_id is not None else None
    store = _as_store(conn)
    acct = store.get_account(code) or store.insert_account(code, kind, owner, bool(non_negative))
    if acct.kind != kind or (owner is not None and acct.owner_user_id != owner):
        raise Conflict("ledger account exists with a different kind/owner", code=code)
    return acct


def _ensure_default_accounts(store: LedgerStore, codes: Iterable[str]) -> None:
    for code in set(codes):
        spec = default_account_spec(code)
        if spec and store.get_account(code) is None:
            kind, owner, non_neg = spec
            store.insert_account(code, kind, owner, non_neg)


def post_transaction(conn: Any, idempotency_key: str, kind: str, memo: str | None,
                     entries: Iterable[Any], created_by: str, *, auto_create: bool = True) -> PostedTx:
    """Post one balanced transaction (idempotent on ``idempotency_key``).

    ``entries``: (account_code, amount_micro) pairs (+debit/−credit) or objects with ``account_code`` and
    ``amount_micro``. With ``auto_create`` the per-user accounts (user:{uuid}:fee_balance,
    creator:{uuid}:payable, referrer:{uuid}:payable) are created on first use; platform accounts are seeded.
    Returns the PostedTx (``created`` False on an idempotent replay).
    Raises ValidationFailed, Conflict (same key, different content), InsufficientBalance, NotFound."""
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        raise ValidationFailed("idempotency_key must be 1..200 chars")
    if not isinstance(kind, str) or not _TX_KIND_RE.match(kind):
        raise ValidationFailed("invalid ledger transaction kind", kind=kind)
    if not isinstance(created_by, str) or not 1 <= len(created_by) <= 200:
        raise ValidationFailed("created_by must be 1..200 chars")
    canon = normalize_entries(entries)
    store = _as_store(conn)

    existing = store.get_tx_by_key(idempotency_key)
    if existing is not None:
        return _same_or_conflict(existing, kind, canon)

    if auto_create:
        _ensure_default_accounts(store, (c for c, _ in canon))
    _precheck_balances(store, kind, canon)
    return store.insert_tx(idempotency_key, kind, memo, created_by, canon)


def _same_or_conflict(existing: PostedTx, kind: str, canon: tuple[tuple[str, int], ...]) -> PostedTx:
    if existing.kind == kind and tuple(sorted(existing.entries)) == canon:
        if existing.created:
            from dataclasses import replace

            return replace(existing, created=False)
        return existing
    raise Conflict("idempotency key already used with different content", idempotency_key=existing.idempotency_key)


def _precheck_balances(store: LedgerStore, kind: str, canon: Sequence[tuple[str, int]]) -> None:
    """Friendly early InsufficientBalance; the DB re-checks authoritatively under the ledger lock."""
    if kind in OVERDRAFT_KINDS:
        return
    deltas: dict[str, int] = {}
    for code, amt in canon:
        deltas[code] = deltas.get(code, 0) + amt
    for code, raw_delta in deltas.items():
        acct = store.get_account(code)
        if acct is None:
            raise NotFound("unknown ledger account", code=code)
        if not acct.non_negative:
            continue
        sign = 1 if acct.kind in DEBIT_NORMAL else -1
        delta = sign * raw_delta
        if delta >= 0:
            continue
        after = sign * (store.balance(code) or 0) + delta
        if after < 0:
            raise InsufficientBalance("insufficient balance", account=code, shortfall_micro=-after)


def get_balance(conn: Any, code: str, *, missing_ok: bool = True) -> int:
    """RAW signed balance Σ amount_micro (+debit/−credit). An account that does not exist yet has balance 0
    (e.g. a user who never topped up) unless ``missing_ok=False`` (then NotFound)."""
    validate_account_code(code)
    bal = _as_store(conn).balance(code)
    if bal is None:
        if not missing_ok:
            raise NotFound("unknown ledger account", code=code)
        return 0
    return int(bal)


def normal_balance(conn: Any, code: str) -> int:
    """Sign-adjusted balance: raw for asset/expense, −raw for liability/revenue. 0 for missing accounts."""
    store = _as_store(conn)
    acct = store.get_account(validate_account_code(code))
    if acct is None:
        return 0
    raw = store.balance(code) or 0
    return raw if acct.kind in DEBIT_NORMAL else -raw


def available_fee_balance(conn: Any, user_id: str) -> int:
    """User-facing fee balance in micro-USD (may be negative after profit share / chargebacks = debt)."""
    return -get_balance(conn, fee_balance_account(user_id))


def get_transaction(conn: Any, idempotency_key: str) -> PostedTx | None:
    return _as_store(conn).get_tx_by_key(idempotency_key)


# ------------------------------------------------------------------------------------------------ port adapters

class LedgerService:
    """Stateless adapter for ``app.api.deps.LedgerPort`` (conn passed per call)."""

    def post(self, conn: Any, *, idempotency_key: str, kind: str, memo: str, entries: list[tuple[str, int]],
             created_by: str) -> str:
        return post_transaction(conn, idempotency_key, kind, memo, entries, created_by).id

    def balance(self, conn: Any, account_code: str) -> int:
        return get_balance(conn, account_code)

    def post_tx(self, conn: Any, *, idempotency_key: str, kind: str, memo: str | None, entries: Iterable[Any],
                created_by: str) -> PostedTx:
        return post_transaction(conn, idempotency_key, kind, memo, entries, created_by)

    def ensure_account(self, conn: Any, code: str, kind: str, owner_user_id: str | None = None,
                       non_negative: bool | None = None) -> Account:
        return ensure_account(conn, code, kind, owner_user_id, non_negative=non_negative)


class BoundLedger:
    """Adapter for ``app.execution.ports.LedgerPoster`` / ``LedgerReader`` bound to one connection/store."""

    def __init__(self, conn: Any) -> None:
        self._store = _as_store(conn)

    def post_transaction(self, *, idempotency_key: str, kind: str, memo: str, lines: Sequence[Any],
                         created_by: str) -> tuple[str, bool]:
        tx = post_transaction(self._store, idempotency_key, kind, memo, lines, created_by)
        return tx.id, tx.created

    def has_transaction(self, idempotency_key: str) -> bool:
        return self._store.get_tx_by_key(idempotency_key) is not None

    def balance(self, account_code: str) -> int:
        return get_balance(self._store, account_code)

