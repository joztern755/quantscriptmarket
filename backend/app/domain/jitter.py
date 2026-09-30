"""Per-user deterministic-random execution delay and fair ordering (SPEC §5.8 privacy + fairness).

Both use HMAC-SHA256 keyed with a server-side secret salt (Secret Manager), so they are unpredictable to
outsiders (subscriber orders cannot be clustered by a fixed delay pattern) yet reproducible for audits and
safe across executor retries (the same bar gives the same delay and the same order).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from collections.abc import Iterable
from typing import TypeVar

from ._common import require_aware, require_non_negative

__all__ = ["MIN_SALT_BYTES", "delay_seconds", "due_at", "bar_seed", "fair_order"]

MIN_SALT_BYTES = 16
T = TypeVar("T")


def _check_key(name: str, key: bytes) -> bytes:
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError(f"{name} must be bytes")
    if len(key) < MIN_SALT_BYTES:
        raise ValueError(f"{name} must be at least {MIN_SALT_BYTES} bytes")
    return bytes(key)


def _bar_key(bar_close: datetime) -> str:
    return require_aware("bar_close", bar_close).isoformat(timespec="seconds")


def _mac(key: bytes, msg: str) -> int:
    return int.from_bytes(hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest(), "big")


def delay_seconds(user_id: str, bar_close: datetime, salt: bytes, jitter_max_seconds: int) -> int:
    """Delay in [0, jitter_max_seconds] (inclusive) for `user_id` at `bar_close`.
    HMAC-SHA256(salt, "delay|<user_id>|<bar_close UTC ISO>") mod (max+1); the 256-bit value makes modulo bias
    negligible (< 2^-240)."""
    key = _check_key("salt", salt)
    mx = require_non_negative("jitter_max_seconds", jitter_max_seconds)
    if mx == 0:
        return 0
    return _mac(key, f"delay|{user_id}|{_bar_key(bar_close)}") % (mx + 1)


def due_at(user_id: str, bar_close: datetime, salt: bytes, jitter_max_seconds: int) -> datetime:
    """Earliest instant the executor may act for this user on this bar."""
    return require_aware("bar_close", bar_close) + timedelta(seconds=delay_seconds(user_id, bar_close, salt, jitter_max_seconds))


def bar_seed(salt: bytes, bar_close: datetime) -> bytes:
    """Per-bar ordering seed derived from the secret salt (so the order changes every bar)."""
    key = _check_key("salt", salt)
    return hmac.new(key, f"order|{_bar_key(bar_close)}".encode("utf-8"), hashlib.sha256).digest()


def fair_order(ids: Iterable[T], seed: bytes) -> list[T]:
    """Deterministic shuffle: sort by HMAC-SHA256(seed, str(id)) (ties → str(id)). Duplicates are kept."""
    key = _check_key("seed", seed)
    items = list(ids)
    return sorted(items, key=lambda x: (_mac(key, str(x)), str(x)))
