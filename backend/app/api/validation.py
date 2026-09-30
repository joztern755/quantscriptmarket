"""Stdlib-only input validation helpers shared by schemas and routers.

Kept free of FastAPI/pydantic so it is unit-testable with `python -m unittest` and reusable by other layers.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from app.money import MICRO

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
CHAIN_ID_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
# Hyperliquid coin: validator perp ("BTC", "kPEPE") or builder dex perp ("xyz:SILVER").
COIN_RE = re.compile(r"^(?:[a-z0-9]{1,12}:)?[A-Za-z0-9]{1,20}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{16,128}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9\-_.]{8,64}$")
USD_STRING_RE = re.compile(r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?$")

#: Hard ceiling for any single user-entered amount ($100M). Sanity bound, not a business limit.
MAX_AMOUNT_MICRO = 100_000_000 * MICRO


class InputError(ValueError):
    """Raised by helpers; schemas turn it into a 422 validation error."""


def normalize_address(value: str) -> str:
    if not isinstance(value, str) or not ADDRESS_RE.match(value):
        raise InputError("invalid address: expected 0x + 40 hex chars")
    return value.lower()


def usd_string_to_micro(value: str) -> int:
    """Exact decimal USD string (≤ 6 dp, no sign, no exponent) → integer micro. Never rounds silently."""
    if not isinstance(value, str) or not USD_STRING_RE.match(value):
        raise InputError("invalid amount: expected a decimal string like '12.50' with at most 6 decimals")
    try:
        d = Decimal(value)
    except InvalidOperation as e:  # pragma: no cover - regex already guarantees parseability
        raise InputError("invalid amount") from e
    micro = d * MICRO
    if micro != micro.to_integral_value():
        raise InputError("invalid amount: more precision than 1e-6")
    out = int(micro)
    if out <= 0:
        raise InputError("amount must be positive")
    if out > MAX_AMOUNT_MICRO:
        raise InputError("amount too large")
    return out


def check_micro(value: int, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError("amount_micro must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise InputError("amount must be positive")
    if value > MAX_AMOUNT_MICRO:
        raise InputError("amount too large")
    return value


def micro_to_usd_string(micro: int) -> str:
    """Integer micro → minimal decimal string ("10", "10.5", "0.000001") as Hyperliquid expects."""
    d = (Decimal(micro) / MICRO).normalize()
    s = format(d, "f")
    return s


def tenths_bp_to_percent_string(tenths_bp: int) -> str:
    """Builder fee in tenths of a bp → Hyperliquid maxFeeRate percent string (100 → "0.1%")."""
    d = (Decimal(tenths_bp) / Decimal(1000)).normalize()
    return format(d, "f") + "%"


def parse_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as e:
        raise InputError("invalid id") from e


# ------------------------------------------------------------------------------------------------ pagination
def encode_cursor(ts: datetime, row_id: str) -> str:
    raw = json.dumps({"t": ts.astimezone(timezone.utc).isoformat(), "i": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """Opaque keyset cursor → (created_at, id). Invalid cursors raise InputError (never reach SQL raw)."""
    if not cursor:
        return None
    if len(cursor) > 200:
        raise InputError("invalid cursor")
    try:
        pad = "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
        ts = datetime.fromisoformat(obj["t"])
        if ts.tzinfo is None:
            raise ValueError("naive")
        return ts, parse_uuid(obj["i"])
    except (binascii.Error, ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise InputError("invalid cursor") from e


# ------------------------------------------------------------------------------------------------ hashing
def hash_identifier(value: str | None, pepper: bytes | str, *, domain: str = "ip") -> str | None:
    """HMAC-SHA256(pepper, domain || 0x00 || value) hex — identical to app.security.audit.pepper_hash, so
    ip_hash values in consents and audit_log correlate. IPs are normalised first (IPv4-mapped IPv6 → IPv4)."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    if domain == "ip":
        try:
            addr = ipaddress.ip_address(raw)
            if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
                addr = addr.ipv4_mapped
            raw = addr.compressed
        except ValueError:
            raw = "invalid:" + raw[:64]
    key = pepper.encode() if isinstance(pepper, str) else pepper
    return hmac.new(key, domain.encode() + b"\x00" + raw.encode("utf-8"), hashlib.sha256).hexdigest()


def request_fingerprint(method: str, path: str, body: bytes) -> str:
    """Hash of an idempotent request (so a reused key with a different payload is rejected)."""
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\0")
    h.update(path.encode())
    h.update(b"\0")
    h.update(body)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------ wallet verify (SIWE)
SIWE_SUFFIX = " wants you to sign in with your Ethereum account:"
_SIWE_FIELDS = {"URI", "Version", "Chain ID", "Nonce", "Issued At", "Expiration Time", "Not Before", "Request ID"}
NONCE_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")


def parse_siwe(message: str) -> dict[str, str]:
    """Parse an EIP-4361 message into {domain, address, URI, Version, Chain ID, Nonce, Issued At, …}.
    Strict: LF line endings only, each field at most once, required fields present. Statement is ignored."""
    if not isinstance(message, str) or "\r" in message or len(message) > 2000:
        raise InputError("malformed sign-in message")
    lines = message.split("\n")
    if len(lines) < 7 or not lines[0].endswith(SIWE_SUFFIX):
        raise InputError("not an EIP-4361 sign-in message")
    out: dict[str, str] = {"domain": lines[0][: -len(SIWE_SUFFIX)], "address": lines[1].strip()}
    if not ADDRESS_RE.match(out["address"]):
        raise InputError("sign-in message has no valid address")
    in_resources = False
    for line in lines[2:]:
        if in_resources:
            if line.startswith("- "):
                continue
            in_resources = False
        if line == "Resources:":
            in_resources = True
            continue
        key, sep, value = line.partition(": ")
        if sep and key in _SIWE_FIELDS:
            if key in out:
                raise InputError(f"duplicate field {key}")
            out[key] = value.strip()
    for req in ("URI", "Version", "Chain ID", "Nonce", "Issued At"):
        if req not in out:
            raise InputError(f"sign-in message missing {req}")
    if out["Version"] != "1" or not out["Chain ID"].isdigit() or not NONCE_RE.match(out["Nonce"]):
        raise InputError("unsupported sign-in message fields")
    return out


def parse_issued_at(value: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise InputError("invalid timestamp") from e
    if ts.tzinfo is None:
        raise InputError("timestamp must include a timezone")
    return ts.astimezone(timezone.utc)
