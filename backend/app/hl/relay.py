"""``/exchange`` relay fallback for USER-SIGNED Hyperliquid actions (``POST /v1/hl/exchange-relay``).

Why: the browser normally POSTs the user's signed action straight to ``https://api.hyperliquid.xyz/exchange``. If
Hyperliquid's CORS policy (UNVERIFIED — DEPLOY §16, G16) or a network path blocks that, the web core
(``web/src/core/hl.ts``) falls back to this relay, which forwards the body UNCHANGED. The server never signs and
never holds a key; it only carries a signature the user's own wallet produced.

What may be relayed — ONLY these three user-signed action types, each validated field by field against what the
server expects, and only when the EIP-712 signer is one of the CALLER's verified wallets:

* ``approveAgent``: ``agentAddress`` = one of the caller's PENDING agents (``agent_keys.status = pending_approval``)
  whose master is the signer; ``agentName`` = that agent's name.
* ``approveBuilderFee``: ``builder`` = our configured builder; ``maxFeeRate`` ≤ the configured builder fee.
* ``usdSend``: ``destination`` = our treasury (fee-balance deposit); ``amount`` a positive USDC amount (≤ 6 dp).

Common checks: exact key sets (no ``vaultAddress`` / ``expiresAfter`` / extra fields), ``hyperliquidChain`` = our
network, ``signatureChainId`` a hex chain id, body ``nonce`` = the action's ``nonce`` / ``time`` and within
[now − 15 min, now + 5 min], ``signature`` = {r, s, v} (low-s, v ∈ {27, 28}). Everything else — orders, cancels,
withdrawals, transfers of any other kind — is refused before any network call (``RelayRefused``).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Optional

from app.errors import ExternalServiceError, ValidationFailed

__all__ = ["RELAYABLE", "RelayPolicy", "RelayRefused", "RelayCheck", "validate_relay", "forward_exchange",
           "fee_rate_tenths_bp", "charge_relay_budget"]

RELAYABLE = ("approveAgent", "approveBuilderFee", "usdSend")
_FIELDS = {
    "approveAgent": {"type", "signatureChainId", "hyperliquidChain", "agentAddress", "agentName", "nonce"},
    "approveBuilderFee": {"type", "signatureChainId", "hyperliquidChain", "maxFeeRate", "builder", "nonce"},
    "usdSend": {"type", "signatureChainId", "hyperliquidChain", "destination", "amount", "time"},
}
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
_CHAIN_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_FEE_RE = re.compile(r"^(\d{1,3})(?:\.(\d{1,8}))?%$")
_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]{0,11})(\.[0-9]{0,5}[1-9])?$")


class RelayRefused(ValidationFailed):
    """The body is not something we relay (wrong type, field or signer). Nothing was sent."""


@dataclass(frozen=True)
class RelayPolicy:
    hyperliquid_chain: str                  # "Mainnet" | "Testnet"
    builder_address: str                    # lower-case
    max_builder_fee_tenths_bp: int
    treasury_address: str                   # lower-case
    max_nonce_age_ms: int = 15 * 60_000
    max_nonce_ahead_ms: int = 5 * 60_000


@dataclass(frozen=True)
class RelayCheck:
    kind: str
    signer: str
    nonce: int
    body: dict                              # exactly what is forwarded
    detail: dict                            # audit-friendly summary (no signature)


def fee_rate_tenths_bp(rate: str) -> Optional[int]:
    """Hyperliquid percent string → tenths of a bp, rounded UP (a larger rate can never slip through)."""
    m = _FEE_RE.fullmatch(rate or "")
    if not m:
        return None
    frac = (m.group(2) or "").ljust(8, "0")
    scaled = int(m.group(1) + frac)          # percent × 1e8; 1 tenth-bp = 0.001 % = 100_000
    return -(-scaled // 100_000)


def _refuse(msg: str, **kw: Any) -> RelayRefused:
    return RelayRefused(msg, **kw)


def _addr(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _ADDR_RE.fullmatch(v):
        raise _refuse(f"bad {what} address")
    return v.lower()


def _int(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise _refuse(f"{what} must be an integer")
    return v


def _recover_signer(kind: str, action: Mapping[str, Any], signature: Mapping[str, Any]) -> str:
    from app.api.ethsig import recover_address
    from app.hl import typed_data as td

    primary, types = {"approveAgent": (td.APPROVE_AGENT_PRIMARY, td.APPROVE_AGENT_TYPES),
                      "approveBuilderFee": (td.APPROVE_BUILDER_FEE_PRIMARY, td.APPROVE_BUILDER_FEE_TYPES),
                      "usdSend": (td.USD_SEND_PRIMARY, td.USD_SEND_TYPES)}[kind]
    msg = {f["name"]: action[f["name"]] for f in types}
    typed = td.user_signed_typed_data(primary, types, msg, str(action["signatureChainId"]))
    digest = td.hash_typed_data(typed)
    r, s, v = signature["r"], signature["s"], signature["v"]
    raw = int(r, 16).to_bytes(32, "big") + int(s, 16).to_bytes(32, "big") + bytes([v])
    try:
        return recover_address(digest, raw)
    except ValueError as e:
        raise _refuse("invalid signature", error=str(e)[:80]) from None


def validate_relay(body: Mapping[str, Any], *, policy: RelayPolicy, now_ms: int, verified_wallets: Iterable[str],
                   pending_agents: Mapping[str, Mapping[str, Any]]) -> RelayCheck:
    """``pending_agents``: {agent_address (lower): {"master_address", "agent_name"}} of the CALLER's pending agents;
    ``verified_wallets``: the caller's verified master addresses. Raises ``RelayRefused``; nothing is sent."""
    if not isinstance(body, Mapping) or set(body) != {"action", "nonce", "signature"}:
        raise _refuse("body must be exactly {action, nonce, signature}")
    action, signature = body["action"], body["signature"]
    if not isinstance(action, Mapping):
        raise _refuse("action must be an object")
    kind = action.get("type")
    if kind not in RELAYABLE:
        raise _refuse("only approveAgent, approveBuilderFee and usdSend can be relayed", type=str(kind)[:40])
    if set(action) != _FIELDS[kind]:
        raise _refuse("unexpected or missing action fields", type=kind)
    if action.get("hyperliquidChain") != policy.hyperliquid_chain:
        raise _refuse("wrong hyperliquidChain", expected=policy.hyperliquid_chain)
    chain = action.get("signatureChainId")
    if not isinstance(chain, str) or not _CHAIN_RE.fullmatch(chain) or int(chain, 16) == 0:
        raise _refuse("bad signatureChainId")
    nonce = _int(body["nonce"], "nonce")
    inner = _int(action["time" if kind == "usdSend" else "nonce"], "action nonce")
    if inner != nonce:
        raise _refuse("nonce does not match the signed action")
    if not now_ms - policy.max_nonce_age_ms <= nonce <= now_ms + policy.max_nonce_ahead_ms:
        raise _refuse("nonce is stale or in the future; sign again")
    if (not isinstance(signature, Mapping) or set(signature) != {"r", "s", "v"}
            or not isinstance(signature.get("r"), str) or not _HEX_RE.fullmatch(signature["r"])
            or not isinstance(signature.get("s"), str) or not _HEX_RE.fullmatch(signature["s"])
            or isinstance(signature.get("v"), bool) or signature.get("v") not in (27, 28)):
        raise _refuse("bad signature (need hex r, s and v ∈ {27, 28})")

    detail: dict[str, Any] = {"type": kind, "nonce": nonce}
    if kind == "approveAgent":
        agent = _addr(action["agentAddress"], "agent")
        pending = pending_agents.get(agent)
        if pending is None:
            raise _refuse("agentAddress is not one of your pending agents")
        if action.get("agentName") != pending.get("agent_name"):
            raise _refuse("agentName does not match your pending agent")
        detail.update(agent_address=agent)
    elif kind == "approveBuilderFee":
        if not policy.builder_address or _addr(action["builder"], "builder") != policy.builder_address.lower():
            raise _refuse("builder is not our builder address")
        rate = action.get("maxFeeRate")
        tenths = fee_rate_tenths_bp(rate) if isinstance(rate, str) else None
        if tenths is None or tenths <= 0 or tenths > policy.max_builder_fee_tenths_bp:
            raise _refuse("maxFeeRate above the configured builder fee", max_tenths_bp=policy.max_builder_fee_tenths_bp)
        detail.update(max_fee_tenths_bp=tenths)
    else:
        if not policy.treasury_address or _addr(action["destination"], "destination") != policy.treasury_address.lower():
            raise _refuse("usdSend can only be relayed to our treasury")
        amount = action.get("amount")
        try:
            ok = isinstance(amount, str) and _AMOUNT_RE.fullmatch(amount) is not None and Decimal(amount) > 0
        except InvalidOperation:
            ok = False
        if not ok:
            raise _refuse("bad usdSend amount")
        detail.update(amount=amount)

    signer = _recover_signer(kind, action, signature)
    wallets = {str(w).lower() for w in verified_wallets}
    if signer not in wallets:
        raise _refuse("the signature is not from one of your verified wallets")
    if kind == "approveAgent" and str(pending_agents[detail["agent_address"]].get("master_address", "")).lower() != signer:
        raise _refuse("the agent must be approved by its own master wallet")
    detail.update(signer=signer)
    fwd = {"action": dict(action), "nonce": nonce, "signature": dict(signature)}
    return RelayCheck(kind=kind, signer=signer, nonce=nonce, body=fwd, detail=detail)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **kw: Any) -> None:   # never follow a redirect with a signed body
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def charge_relay_budget(budget: Any, limits: Any, *, max_wait_seconds: float,
                        monotonic: Callable[[], float] = time.monotonic,
                        sleep: Callable[[float], None] = time.sleep) -> None:
    """Charge one relayed ``/exchange`` request to the SHARED per-egress-IP Hyperliquid budget BEFORE it is sent
    (``app.hl.budget``; Hyperliquid counts /exchange against the same per-IP weight limit as /info). Low-priority
    ``jobs`` pool, waiting at most ``max_wait_seconds``; no room → ``HlBudgetExhausted`` and nothing may be sent.
    ``budget=None`` (no database / shared budget disabled) → no accounting."""
    if budget is None or limits is None:
        return
    from app.hl.budget import POOL_JOBS, HlBudgetExhausted

    weight = int(getattr(limits, "exchange_weight", 1))
    if not budget.acquire_wait(weight, POOL_JOBS, deadline=monotonic() + float(max_wait_seconds),
                               monotonic=monotonic, sleep=sleep):
        raise HlBudgetExhausted("hyperliquid rate budget exhausted", type="exchange", pool=POOL_JOBS)


def forward_exchange(url: str, body: Mapping[str, Any], *, timeout: float = 10.0, max_bytes: int = 64 * 1024,
                     opener: Callable[..., Any] | None = None) -> tuple[int, Any]:
    """POST ``body`` to Hyperliquid ``/exchange`` exactly once (no retry: a user-signed action is not idempotent
    from our side — Hyperliquid dedupes nonces). Returns (HTTP status, parsed JSON or text)."""
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1")) or not url.endswith("/exchange"):
        raise ValidationFailed("relay target must be the https Hyperliquid /exchange endpoint")
    data = json.dumps(dict(body), separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    open_ = opener or _OPENER.open
    try:
        with open_(req, timeout=timeout) as r:  # noqa: S310 - URL from config
            status, raw = int(getattr(r, "status", 200)), r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        status, raw = int(e.code), (e.read(max_bytes + 1) or b"")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ExternalServiceError("hyperliquid exchange unreachable", error=type(e).__name__) from None
    if len(raw) > max_bytes:
        raise ExternalServiceError("hyperliquid exchange response too large")
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text[:1000]
