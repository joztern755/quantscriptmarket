"""EIP-712 payloads for Hyperliquid USER-SIGNED actions (SPEC §6): ApproveAgent, ApproveBuilderFee, UsdSend.

Flow (preferred): the API returns ``{typed_data, action, nonce}``; the browser asks the user's wallet for
``eth_signTypedData_v4(typed_data)`` and POSTs ``{action, nonce, signature}`` DIRECTLY to Hyperliquid
``/exchange`` (``exchange_payload`` shows the exact body). The server never holds a user signature; it confirms
the effect via the info endpoint instead (``extraAgents`` / ``maxBuilderFee`` / treasury ledger updates — see
``app.hl.readers`` and ``app.hl.deposits``). ``exchange_payload`` is also usable for a server relay fallback
(UNVERIFIED here: whether api.hyperliquid.xyz/exchange sends CORS headers for our origin — test before launch;
if not, relay through the API with this exact body).

Encoding facts (from SPEC §6 and the official SDK's ``signing.py``, recalled — source not reachable here):
- domain = {name "HyperliquidSignTransaction", version "1", chainId = int(signatureChainId), verifyingContract 0x0}
- ``signatureChainId`` = hex chain id of the wallet at signing time (any chain; SDK uses 0x66eee);
  ``hyperliquidChain`` = "Mainnet" | "Testnet" (replay protection between environments).
- The wallet signs ONLY the typed fields; the action additionally carries ``type`` and ``signatureChainId``.
- approveAgent: ``agentName`` is signed as a string; our named agent is "aijalon". Named agents EXPIRE: extraAgents
  returns ``validUntil`` (VERIFIED). Appending " valid_until <ms>" to the name to choose the expiry is UNVERIFIED
  (recalled from docs) — off by default.
- approveBuilderFee: ``maxFeeRate`` is a percent string: 100 tenths-bp → "0.1%" (SDK example uses "0.001%" for
  1 tenth-bp). UNVERIFIED against a live response here; confirm with ``maxBuilderFee`` after the first approval.
- nonce / time = ms timestamp; Hyperliquid rejects nonces far from its clock (window ~(T−2d, T+1d)) or reused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.errors import ValidationFailed
from app.security.keccak import keccak256

__all__ = [
    "DOMAIN_NAME", "DOMAIN_VERSION", "ZERO_ADDRESS", "EIP712_DOMAIN_TYPES",
    "APPROVE_AGENT_PRIMARY", "APPROVE_AGENT_TYPES", "APPROVE_BUILDER_FEE_PRIMARY", "APPROVE_BUILDER_FEE_TYPES",
    "USD_SEND_PRIMARY", "USD_SEND_TYPES", "DEFAULT_AGENT_NAME",
    "UserSignedRequest", "normalize_chain_id", "fee_rate_percent", "user_signed_typed_data",
    "approve_agent_request", "approve_builder_fee_request", "usd_send_request", "usd_send_typed_data",
    "exchange_payload", "hash_struct", "hash_typed_data",
]

DOMAIN_NAME = "HyperliquidSignTransaction"
DOMAIN_VERSION = "1"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEFAULT_AGENT_NAME = "aijalon"

EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
APPROVE_AGENT_PRIMARY = "HyperliquidTransaction:ApproveAgent"
APPROVE_AGENT_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]
APPROVE_BUILDER_FEE_PRIMARY = "HyperliquidTransaction:ApproveBuilderFee"
APPROVE_BUILDER_FEE_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "maxFeeRate", "type": "string"},
    {"name": "builder", "type": "address"},
    {"name": "nonce", "type": "uint64"},
]
USD_SEND_PRIMARY = "HyperliquidTransaction:UsdSend"
USD_SEND_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "destination", "type": "string"},
    {"name": "amount", "type": "string"},
    {"name": "time", "type": "uint64"},
]

_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_HEX32_RE = re.compile(r"^0x[0-9a-f]{1,64}$")
_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]*[1-9])?$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,16}$")
MAX_BUILDER_FEE_TENTHS_BP_PERP = 100  # 0.1 %


@dataclass(frozen=True)
class UserSignedRequest:
    typed_data: dict      # hand to eth_signTypedData_v4
    action: dict          # POST /exchange {action, nonce, signature}; must match the signed message
    nonce: int            # ms; equals action["nonce"] (or action["time"] for usdSend)

    def public_view(self) -> dict:
        return {"typed_data": self.typed_data, "action": self.action, "nonce": self.nonce}


def _addr(v: Any, what: str) -> str:
    s = str(v or "").strip().lower()
    if not _ADDR_RE.fullmatch(s):
        raise ValidationFailed(f"bad {what} address")
    return s


def _nonce(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not (1_500_000_000_000 <= v < 10_000_000_000_000):
        raise ValidationFailed("nonce must be a millisecond timestamp")
    return v


def normalize_chain_id(v: int | str) -> str:
    """Wallet chain id → lower-case hex string ("0xa4b1"). Accepts int or hex string."""
    if isinstance(v, bool):
        raise ValidationFailed("bad signature chain id")
    if isinstance(v, int):
        n = v
    else:
        s = str(v).strip().lower()
        if not re.fullmatch(r"0x[0-9a-f]{1,16}", s):
            raise ValidationFailed("bad signature chain id")
        n = int(s, 16)
    if not 0 < n < 2 ** 64:
        raise ValidationFailed("bad signature chain id")
    return hex(n)


def fee_rate_percent(tenths_bp: int) -> str:
    """Builder fee in tenths of a bp → Hyperliquid percent string: 100 → "0.1%", 10 → "0.01%", 1 → "0.001%"."""
    if isinstance(tenths_bp, bool) or not isinstance(tenths_bp, int) or not 1 <= tenths_bp <= MAX_BUILDER_FEE_TENTHS_BP_PERP:
        raise ValidationFailed("builder fee must be 1..100 tenths of a bp (perp max 0.1%)")
    pct = Decimal(tenths_bp) / Decimal(1000)  # 1 tenth-bp = 0.001 %
    return format(pct.normalize(), "f") + "%"


def user_signed_typed_data(primary_type: str, types: Sequence[Mapping[str, str]], message: Mapping[str, Any],
                           signature_chain_id: int | str) -> dict:
    chain = normalize_chain_id(signature_chain_id)
    names = [t["name"] for t in types]
    if set(message) != set(names):
        raise ValidationFailed("message fields must equal the typed fields")
    return {
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": int(chain, 16),
                   "verifyingContract": ZERO_ADDRESS},
        "types": {"EIP712Domain": [dict(t) for t in EIP712_DOMAIN_TYPES], primary_type: [dict(t) for t in types]},
        "primaryType": primary_type,
        "message": {n: message[n] for n in names},
    }


def _hl_chain(is_mainnet: bool) -> str:
    return "Mainnet" if is_mainnet else "Testnet"


def approve_agent_request(agent_address: str, *, nonce_ms: int, signature_chain_id: int | str,
                          is_mainnet: bool = True, agent_name: str = DEFAULT_AGENT_NAME,
                          valid_until_ms: int | None = None) -> UserSignedRequest:
    """``approveAgent`` for our per-user agent. Approving a named agent whose name already exists on the account
    replaces it (rotation) — recalled behaviour, UNVERIFIED here."""
    agent = _addr(agent_address, "agent")
    if not _AGENT_NAME_RE.fullmatch(agent_name or ""):
        raise ValidationFailed("agent name must be 1-16 chars [A-Za-z0-9 _-] (unnamed agents are not used)")
    name = agent_name
    if valid_until_ms is not None:
        name = f"{agent_name} valid_until {_nonce(valid_until_ms)}"  # UNVERIFIED suffix syntax
    nonce = _nonce(nonce_ms)
    chain = normalize_chain_id(signature_chain_id)
    msg = {"hyperliquidChain": _hl_chain(is_mainnet), "agentAddress": agent, "agentName": name, "nonce": nonce}
    action = {"type": "approveAgent", "signatureChainId": chain, **msg}
    return UserSignedRequest(user_signed_typed_data(APPROVE_AGENT_PRIMARY, APPROVE_AGENT_TYPES, msg, chain),
                             action, nonce)


def approve_builder_fee_request(builder_address: str, *, nonce_ms: int, signature_chain_id: int | str,
                                is_mainnet: bool = True, max_fee_tenths_bp: int = 100) -> UserSignedRequest:
    builder = _addr(builder_address, "builder")
    nonce = _nonce(nonce_ms)
    chain = normalize_chain_id(signature_chain_id)
    msg = {"hyperliquidChain": _hl_chain(is_mainnet), "maxFeeRate": fee_rate_percent(max_fee_tenths_bp),
           "builder": builder, "nonce": nonce}
    action = {"type": "approveBuilderFee", "signatureChainId": chain, **msg}
    return UserSignedRequest(user_signed_typed_data(APPROVE_BUILDER_FEE_PRIMARY, APPROVE_BUILDER_FEE_TYPES, msg,
                                                    chain), action, nonce)


def _amount(amount: str) -> str:
    if not isinstance(amount, str) or not _AMOUNT_RE.fullmatch(amount) or Decimal(amount) <= 0:
        raise ValidationFailed("amount must be a canonical positive decimal string (e.g. \"10.5\")")
    if len(amount.partition(".")[2]) > 6:
        raise ValidationFailed("USDC amount has at most 6 decimals")
    return amount


def usd_send_typed_data(*, destination: str, amount: str, time_ms: int, signature_chain_id: str,
                        is_mainnet: bool) -> dict:
    """Typed data for ``usdSend`` (signature matches ``app.payments.usdc``'s delegation contract)."""
    msg = {"hyperliquidChain": _hl_chain(is_mainnet), "destination": _addr(destination, "destination"),
           "amount": _amount(amount), "time": _nonce(time_ms)}
    return user_signed_typed_data(USD_SEND_PRIMARY, USD_SEND_TYPES, msg, signature_chain_id)


def usd_send_request(destination: str, amount: str, *, time_ms: int, signature_chain_id: int | str,
                     is_mainnet: bool = True) -> UserSignedRequest:
    chain = normalize_chain_id(signature_chain_id)
    typed = usd_send_typed_data(destination=destination, amount=amount, time_ms=time_ms, signature_chain_id=chain,
                                is_mainnet=is_mainnet)
    action = {"type": "usdSend", "signatureChainId": chain, **typed["message"]}
    return UserSignedRequest(typed, action, int(typed["message"]["time"]))


def exchange_payload(action: Mapping[str, Any], signature: Mapping[str, Any]) -> dict:
    """``POST /exchange`` body for a user-signed action: ``{action, nonce, signature:{r,s,v}}``."""
    kind = action.get("type")
    if kind not in ("approveAgent", "approveBuilderFee", "usdSend"):
        raise ValidationFailed("not a supported user-signed action")
    nonce = action.get("time") if kind == "usdSend" else action.get("nonce")
    r, s, v = signature.get("r"), signature.get("s"), signature.get("v")
    if not (isinstance(r, str) and _HEX32_RE.fullmatch(r.lower()) and isinstance(s, str)
            and _HEX32_RE.fullmatch(s.lower()) and isinstance(v, int) and not isinstance(v, bool) and v in (27, 28)):
        raise ValidationFailed("bad signature (need hex r, s and v ∈ {27, 28})")
    return {"action": dict(action), "nonce": _nonce(nonce), "signature": {"r": r.lower(), "s": s.lower(), "v": v}}


# ---------------------------------------------------------------------------------------------------------------
# EIP-712 hashing (generic; used by tests to pin encodings and available for signature checks)
# ---------------------------------------------------------------------------------------------------------------

def _deps(primary: str, types: Mapping[str, Sequence[Mapping[str, str]]], acc: set[str]) -> set[str]:
    if primary in acc or primary not in types:
        return acc
    acc.add(primary)
    for f in types[primary]:
        base = f["type"].split("[", 1)[0]
        if base in types:
            _deps(base, types, acc)
    return acc


def _encode_type(primary: str, types: Mapping[str, Sequence[Mapping[str, str]]]) -> str:
    deps = sorted(_deps(primary, types, set()) - {primary})
    return "".join(f"{t}({','.join(f['type'] + ' ' + f['name'] for f in types[t])})" for t in [primary] + deps)


def _type_hash(primary: str, types: Mapping[str, Sequence[Mapping[str, str]]]) -> bytes:
    return keccak256(_encode_type(primary, types).encode())


def _encode_value(typ: str, value: Any, types: Mapping[str, Sequence[Mapping[str, str]]]) -> bytes:
    if typ.endswith("]"):
        inner = typ[: typ.rindex("[")]
        return keccak256(b"".join(_encode_value(inner, v, types) for v in value))
    if typ in types:
        return hash_struct(typ, value, types)
    if typ == "string":
        return keccak256(str(value).encode())
    if typ == "bytes":
        b = bytes.fromhex(value[2:]) if isinstance(value, str) else bytes(value)
        return keccak256(b)
    if typ == "address":
        return bytes(12) + bytes.fromhex(_addr(value, "typed-data")[2:])
    if typ == "bool":
        return int(bool(value)).to_bytes(32, "big")
    if typ.startswith("uint") or typ.startswith("int"):
        n = int(value, 0) if isinstance(value, str) else int(value)
        bits = int(typ[4:] if typ.startswith("uint") else typ[3:] or 256)
        if typ.startswith("uint") and not 0 <= n < 2 ** bits:
            raise ValidationFailed(f"{typ} out of range")
        return (n % 2 ** 256).to_bytes(32, "big")
    if typ.startswith("bytes"):
        b = bytes.fromhex(value[2:]) if isinstance(value, str) else bytes(value)
        return b.ljust(32, b"\0")
    raise ValidationFailed(f"unsupported EIP-712 type {typ}")


def hash_struct(primary: str, data: Mapping[str, Any], types: Mapping[str, Sequence[Mapping[str, str]]]) -> bytes:
    enc = _type_hash(primary, types) + b"".join(_encode_value(f["type"], data[f["name"]], types)
                                                for f in types[primary])
    return keccak256(enc)


def hash_typed_data(typed: Mapping[str, Any]) -> bytes:
    """EIP-712 digest ``keccak256(0x1901 ‖ domainSeparator ‖ hashStruct(message))``."""
    types = typed["types"]
    domain_sep = hash_struct("EIP712Domain", typed["domain"], types)
    return keccak256(b"\x19\x01" + domain_sep + hash_struct(typed["primaryType"], typed["message"], types))
