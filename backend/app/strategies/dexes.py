"""Trusted perp dexes (SPEC §12 "Trusted builder dexes"; docs/security/REVIEW_TRADING_KEYS.md F1).

A builder-deployed (HIP-3) perp dex is run by its deployer, who controls the oracle price, mark price, 24h volume and
open interest the thin-market guards read (``app.domain.risk``). On an attacker-owned dex those guards can be made to
pass while subscribers buy into the attacker's orders, so strategies may trade only:

* validator perps (dex ``""``, e.g. ``BTC``) — always trusted, cannot be removed; and
* builder dexes on the admin-managed allowlist ``trusted_dexes`` (migration 0012; launch list below).

Enforcement points (each re-reads the table — no caching across requests / ticks):
  API     creator strategy create, version upload, listing proposal, listing approval (``app.api.routers``)
  exec    pre-trade guard ``app.execution.wiring.RiskPlanner`` — a coin on a non-trusted dex is ENTRIES-BLOCKED
          (exits still run so subscribers are never trapped); allowlist unreadable → ``None`` → only validator perps
          may open (fail closed)
  signals in-house signal ingestion refuses records for coins on a non-trusted dex (``app.jobs_data.signals``)

Admin: ONE admin adds a dex (step-up, audit-logged); removal is immediate (soft delete) and pauses new entries on the
dex's markets from the next executor tick. Pure helpers + SQL only (no FastAPI), so every service can import it.
"""
from __future__ import annotations

import re
from typing import Any, Collection, Iterable, Optional

from app.errors import ValidationFailed

__all__ = [
    "VALIDATOR_DEX", "LAUNCH_TRUSTED_DEXES", "DEX_NAME_RE", "dex_of", "normalize_dex", "is_trusted_coin",
    "untrusted_markets", "untrusted_dexes", "require_trusted", "load_trusted", "list_dexes", "add_dex", "remove_dex",
    "SQL_ACTIVE", "SQL_LIST",
]

VALIDATOR_DEX = ""
#: SPEC §12 launch allowlist (the 10 builder dexes live on 30 Sep 2026). Seeded by migrations/0012; the table is the
#: source of truth afterwards — this constant is documentation and a test fixture, never a runtime fallback.
LAUNCH_TRUSTED_DEXES: tuple[str, ...] = ("xyz", "flx", "vntl", "hyna", "km", "abcd", "cash", "para", "mkts", "io")
#: same shape as the coin prefix accepted by app.sandbox.validate._COIN_RE and the DB CHECK
DEX_NAME_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")

SQL_ACTIVE = "SELECT dex FROM trusted_dexes WHERE removed_at IS NULL"
SQL_LIST = """SELECT dex, created_at, updated_at, added_by, reason, removed_at, removed_by, removal_reason,
                     (removed_at IS NULL) AS active
                FROM trusted_dexes ORDER BY (removed_at IS NULL) DESC, dex"""


def dex_of(coin: str) -> str:
    """``"xyz:SILVER"`` → ``"xyz"``; ``"BTC"`` → ``""`` (validator dex)."""
    return coin.split(":", 1)[0] if isinstance(coin, str) and ":" in coin else VALIDATOR_DEX


def normalize_dex(raw: Any) -> str:
    """Validate an admin-supplied builder dex name (the validator dex is implicit and never added/removed)."""
    if not isinstance(raw, str):
        raise ValidationFailed("dex must be a string")
    d = raw.strip()
    if d.endswith(":"):
        d = d[:-1]
    if not DEX_NAME_RE.match(d):
        raise ValidationFailed("dex must be 1–16 lower-case letters/digits starting with a letter (e.g. 'xyz')")
    return d


def is_trusted_coin(coin: str, trusted: Optional[Collection[str]]) -> bool:
    """Validator perps are always trusted. ``trusted=None`` means the allowlist could not be read → fail closed."""
    d = dex_of(coin)
    if d == VALIDATOR_DEX:
        return True
    return trusted is not None and d in trusted


def untrusted_markets(coins: Iterable[str], trusted: Optional[Collection[str]]) -> list[str]:
    return [c for c in coins if not is_trusted_coin(c, trusted)]


def untrusted_dexes(coins: Iterable[str], trusted: Optional[Collection[str]]) -> list[str]:
    return sorted({dex_of(c) for c in untrusted_markets(coins, trusted)})


def require_trusted(coins: Iterable[str], trusted: Optional[Collection[str]], *, what: str = "markets") -> None:
    """Raise ``ValidationFailed`` (422) when any coin is on a dex outside the allowlist."""
    coins = list(coins)
    bad = untrusted_markets(coins, trusted)
    if bad:
        raise ValidationFailed(
            f"{what} on builder dexes that are not on the trusted allowlist (an admin must approve the dex first)",
            reason="untrusted_dex", markets=bad[:20], dexes=untrusted_dexes(bad, trusted)[:20])


# ------------------------------------------------------------------------------------------------------ SQL
def _rows(runner: Any, sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """``runner``: anything with ``fetchall(sql, params)`` (``SqlAlchemyRunner``, ``PgDatabase``, tests' psql
    runner) or ``all(sql, **params)``."""
    if hasattr(runner, "fetchall"):
        return [dict(r) for r in runner.fetchall(sql, params or {})]
    return [dict(r) for r in runner.all(sql, **(params or {}))]


def load_trusted(runner: Any) -> frozenset[str]:
    """Active builder dexes (+ ``""``). Raises on database errors — callers decide how to fail closed."""
    return frozenset(str(r["dex"]) for r in _rows(runner, SQL_ACTIVE)) | {VALIDATOR_DEX}


def list_dexes(runner: Any) -> list[dict[str, Any]]:
    return _rows(runner, SQL_LIST)


def add_dex(runner: Any, dex: str, *, by: str, reason: str) -> Optional[dict[str, Any]]:
    """Insert, or re-activate a removed dex. Returns the row, or None when it is already active."""
    return next(iter(_rows(runner, """
        INSERT INTO trusted_dexes (dex, added_by, reason) VALUES (CAST(:d AS text), CAST(:by AS text), CAST(:r AS text))
        ON CONFLICT (dex) DO UPDATE SET added_by = EXCLUDED.added_by, reason = EXCLUDED.reason,
               created_at = now(), removed_at = NULL, removed_by = NULL, removal_reason = NULL
         WHERE trusted_dexes.removed_at IS NOT NULL
        RETURNING dex, created_at, updated_at, added_by, reason, removed_at, removed_by, removal_reason,
                  (removed_at IS NULL) AS active""", {"d": dex, "by": by[:200], "r": reason[:500]})), None)


def remove_dex(runner: Any, dex: str, *, by: str, reason: str) -> Optional[dict[str, Any]]:
    """Soft-remove an ACTIVE builder dex. Returns the row, or None when unknown / already removed."""
    if dex == VALIDATOR_DEX:
        raise ValidationFailed("the validator dex cannot be removed")
    return next(iter(_rows(runner, """
        UPDATE trusted_dexes SET removed_at = now(), removed_by = CAST(:by AS text), removal_reason = CAST(:r AS text)
         WHERE dex = CAST(:d AS text) AND removed_at IS NULL
        RETURNING dex, created_at, updated_at, added_by, reason, removed_at, removed_by, removal_reason,
                  (removed_at IS NULL) AS active""", {"d": dex, "by": by[:200], "r": reason[:500]})), None)
