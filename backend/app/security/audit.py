"""Append-only, hash-chained audit log (SPEC §4 `audit_log`, §5.6). Every security/financial event goes here.

Chain: hash_n = sha256( prev_hash_n || 0x0A || canonical_json(body_n) ), hex, with prev_hash_0 = "0"*64 and
body = {actor, action, target, payload, ip_hash, created_at}. `verify_chain` recomputes it end to end.

IPs are never stored raw: ip_hash = HMAC-SHA256(pepper, normalised_ip). The pepper is a secret (Secret
Manager), passed in by the caller — this module does not read config. The pepper is ``Settings.audit_pepper_b64``
(env AUDIT_PEPPER_B64, >= 32 random bytes, Secret Manager; ``get_settings`` refuses to start in prod without it);
the same pepper hashes consents.ip_hash / user_agent_hash via `pepper_hash`.

Canonical JSON (`canonical_json`): UTF-8, sorted keys, no whitespace, str keys only, and NO floats (money is
integer micro-USD; floats are not deterministic across encoders). Allowed: dict, list/tuple, str, int, bool, None.
Postgres jsonb round-trips these losslessly, so the hash can be recomputed from the stored row.

Postgres sink contract (`DbApiAuditSink`): the audit_log table has a total order for the chain — ``audit_log.seq``
(0001: set to last + 1 by the insert trigger under a transaction-scoped advisory lock, UNIQUE; verify_chain() walks
it and reports gaps). Appends serialise on that lock.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Protocol

__all__ = [
    "GENESIS_HASH", "AuditEvent", "AuditSink", "InMemoryAuditSink", "DbApiAuditSink", "ChainBroken",
    "canonical_json", "hash_ip", "pepper_hash", "compute_hash", "write_audit", "verify_chain",
]

GENESIS_HASH = "0" * 64
_MIN_PEPPER_LEN = 32
_MAX_FIELD_LEN = 256


class ChainBroken(Exception):
    """Audit chain verification failed (tampering or a bug). Page ops."""


def _check_canonical(obj: Any, path: str = "$") -> None:
    if obj is None or isinstance(obj, (str, bool)):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, float):
        raise TypeError(f"float not allowed in canonical JSON at {path}; use integer micro-units or a string")
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_canonical(v, f"{path}[{i}]")
        return
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"non-string key at {path}")
            _check_canonical(v, f"{path}.{k}")
        return
    raise TypeError(f"type {type(obj).__name__} not allowed in canonical JSON at {path}")


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON bytes for hashing/signing. Raises TypeError on floats and non-JSON types."""
    _check_canonical(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def pepper_hash(value: str, pepper: bytes, *, domain: str) -> str:
    """HMAC-SHA256(pepper, domain || 0x00 || value), hex. `domain` separates uses (ip, ua, device...)."""
    if len(pepper) < _MIN_PEPPER_LEN:
        raise ValueError("pepper must be at least 32 bytes")
    return hmac.new(pepper, domain.encode() + b"\x00" + value.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_ip(ip: str | None, pepper: bytes) -> str | None:
    """Normalise (IPv4-mapped IPv6 -> IPv4, compressed IPv6) then HMAC. Unparseable input is hashed verbatim
    (trimmed, <=64 chars) so that garbage still correlates without being stored raw."""
    if ip is None or not str(ip).strip():
        return None
    raw = str(ip).strip()
    try:
        addr = ipaddress.ip_address(raw)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        norm = addr.compressed
    except ValueError:
        norm = "invalid:" + raw[:64]
    return pepper_hash(norm, pepper, domain="ip")


@dataclass(frozen=True)
class AuditEvent:
    actor: str             # "user:<uuid>", "admin:<uuid>", "system:executor", "stripe:webhook"...
    action: str            # dotted verb, e.g. "agent.create", "payout.approve_1", "flag.kill_switch.set"
    target: str            # "subscription:<uuid>", "user:<uuid>", "" if none
    payload: Mapping[str, Any]
    ip_hash: str | None
    created_at: str        # ISO-8601 UTC with microseconds, e.g. 2026-09-30T07:12:12.123456+00:00
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        return {"actor": self.actor, "action": self.action, "target": self.target, "payload": dict(self.payload),
                "ip_hash": self.ip_hash, "created_at": self.created_at}


def compute_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + b"\n" + canonical_json(body)).hexdigest()


class AuditSink(Protocol):
    def append(self, make_event: Callable[[str], AuditEvent]) -> AuditEvent:
        """Atomically: read the last hash, call make_event(prev_hash), persist, return the event."""


class InMemoryAuditSink:
    """Tests / dev."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def append(self, make_event: Callable[[str], AuditEvent]) -> AuditEvent:
        with self._lock:
            ev = make_event(self.events[-1].hash if self.events else GENESIS_HASH)
            self.events.append(ev)
            return ev


class DbApiAuditSink:
    """DB-API 2.0 connection (psycopg: paramstyle %s). Runs inside the caller's transaction; the caller commits
    (so the audit row commits atomically with the action it records)."""

    LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext('aijalon.audit_log'))"
    LAST_SQL = "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1"
    INSERT_SQL = ("INSERT INTO audit_log (actor, action, target, payload, ip_hash, created_at, prev_hash, hash) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)")

    def __init__(self, conn: Any, *, lock_sql: str | None = LOCK_SQL, last_sql: str = LAST_SQL,
                 insert_sql: str = INSERT_SQL) -> None:
        self._conn, self._lock_sql, self._last_sql, self._insert_sql = conn, lock_sql, last_sql, insert_sql

    def append(self, make_event: Callable[[str], AuditEvent]) -> AuditEvent:
        cur = self._conn.cursor()
        try:
            if self._lock_sql:
                cur.execute(self._lock_sql)
            cur.execute(self._last_sql)
            row = cur.fetchone()
            ev = make_event(row[0] if row else GENESIS_HASH)
            cur.execute(self._insert_sql, (ev.actor, ev.action, ev.target, canonical_json(dict(ev.payload)).decode("utf-8"),
                                           ev.ip_hash, ev.created_at, ev.prev_hash, ev.hash))
            return ev
        finally:
            cur.close()


def _check_field(name: str, value: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > _MAX_FIELD_LEN:
        raise ValueError(f"audit {name} must be a non-empty string <= {_MAX_FIELD_LEN} chars")


def write_audit(sink: AuditSink, actor: str, action: str, target: str, payload: Mapping[str, Any] | None,
                ip: str | None, *, pepper: bytes, now: datetime | None = None) -> AuditEvent:
    """Append one audit event. `payload` must be canonical-JSON-safe (no floats) and must not contain secrets
    (keys, tokens, full card data); it is validated before anything is written."""
    _check_field("actor", actor)
    _check_field("action", action)
    _check_field("target", target, allow_empty=True)
    body_payload = dict(payload or {})
    canonical_json(body_payload)  # validate early
    ts = (now or datetime.now(timezone.utc))
    if ts.tzinfo is None:
        raise ValueError("naive datetime; use UTC-aware")
    created_at = ts.astimezone(timezone.utc).isoformat(timespec="microseconds")
    ip_h = hash_ip(ip, pepper)

    def make(prev_hash: str) -> AuditEvent:
        body = {"actor": actor, "action": action, "target": target, "payload": body_payload,
                "ip_hash": ip_h, "created_at": created_at}
        return AuditEvent(actor, action, target, body_payload, ip_h, created_at, prev_hash, compute_hash(prev_hash, body))

    return sink.append(make)


def verify_chain(events: Iterable[AuditEvent], *, start_prev_hash: str = GENESIS_HASH) -> int:
    """Recompute the chain in order. Returns the number of events checked; raises ChainBroken at the first bad link."""
    prev = start_prev_hash
    n = 0
    for n, ev in enumerate(events, start=1):
        if ev.prev_hash != prev:
            raise ChainBroken(f"event #{n}: prev_hash does not link")
        if compute_hash(ev.prev_hash, ev.body()) != ev.hash:
            raise ChainBroken(f"event #{n}: hash mismatch (content altered)")
        prev = ev.hash
    return n
