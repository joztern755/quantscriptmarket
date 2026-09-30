"""Small shared helpers for the domain package (private)."""
from __future__ import annotations

from datetime import datetime, timezone


def require_int(name: str, value: object) -> int:
    """Money/bps values must be real ints (bool and float are rejected: floats are never money)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def require_non_negative(name: str, value: object) -> int:
    v = require_int(name, value)
    if v < 0:
        raise ValueError(f"{name} must be >= 0, got {v}")
    return v


def require_aware(name: str, dt: object) -> datetime:
    """Domain code is UTC-only; naive datetimes are a bug. Returns the value converted to UTC."""
    if not isinstance(dt, datetime):
        raise TypeError(f"{name} must be datetime, got {type(dt).__name__}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return dt.astimezone(timezone.utc)
