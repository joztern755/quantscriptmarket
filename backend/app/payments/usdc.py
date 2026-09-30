"""USDC top-ups of the fee balance on Hyperliquid (SPEC §0, §6 UsdSend, §8 POST /deposits/usdc/typed-data + /confirm).

1. ``build_topup_request`` returns the EIP-712 typed data for a Hyperliquid ``usdSend`` from the user's verified
   master wallet to OUR TREASURY address (destination is taken from settings, never from the client). The
   user signs it in their wallet; the browser (or the API as a relay via ``exchange_body``) POSTs it to
   ``/exchange``.
2. ``POST /deposits/usdc/confirm`` must NOT credit anything on the client's word — it only nudges the
   ``hl/deposits.py`` scanner. Credits come exclusively from on-chain detections via ``credit_from_detection``
   (idempotent on the transfer hash: ``usdc_hl:{hash}``).

USDC credits are ``withdrawable=True`` (they may leave again as USDC via the maker-checker withdrawal flow);
card-funded credits are not (see stripe_pay).
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

from app.alerts.notifier import Alert, Severity
from app.errors import ValidationFailed
from app.money import MICRO, from_micro, fmt_usd, to_micro, usd
from app.payments.instructions import TREASURY_HL_USDC, CreditInstruction, user_fee_balance_account

__all__ = [
    "USD_SEND_TYPES",
    "EIP712_DOMAIN_TYPES",
    "UsdcTopupRequest",
    "build_topup_request",
    "usd_send_typed_data",
    "format_usd_amount",
    "exchange_body",
    "UsdcDetection",
    "coerce_detection",
    "UsdcOutcome",
    "credit_from_detection",
]

_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_CHAIN_ID_RE = re.compile(r"^0x[0-9a-f]{1,16}$")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]
USD_SEND_PRIMARY = "HyperliquidTransaction:UsdSend"
USD_SEND_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "destination", "type": "string"},
    {"name": "amount", "type": "string"},
    {"name": "time", "type": "uint64"},
]


def _addr(a: Any, what: str) -> str:
    s = str(a or "").strip().lower()
    if not _ADDR_RE.fullmatch(s):
        raise ValidationFailed(f"bad {what} address")
    return s


def format_usd_amount(amount_micro: int) -> str:
    """micro-USD -> Hyperliquid decimal string without exponent or trailing zeros: 10_500_000 -> "10.5"."""
    return format(from_micro(amount_micro).normalize(), "f")


def usd_send_typed_data(*, destination: str, amount: str, time_ms: int, signature_chain_id: str,
                        is_mainnet: bool) -> dict:
    """EIP-712 typed data for a Hyperliquid user-signed UsdSend (SPEC §6)."""
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": int(signature_chain_id, 16),
            "verifyingContract": ZERO_ADDRESS,
        },
        "types": {"EIP712Domain": EIP712_DOMAIN_TYPES, USD_SEND_PRIMARY: USD_SEND_TYPES},
        "primaryType": USD_SEND_PRIMARY,
        "message": {
            "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
            "destination": destination,
            "amount": amount,
            "time": time_ms,
        },
    }


def _typed_data_builder() -> Callable[..., dict]:
    """Prefer the shared app.hl.typed_data builder when it exists with a compatible function; else ours."""
    try:
        from app.hl import typed_data as hl_td  # type: ignore
    except Exception:
        return usd_send_typed_data
    fn = getattr(hl_td, "usd_send_typed_data", None)
    if not callable(fn):
        return usd_send_typed_data
    try:  # only delegate if it takes exactly our keyword arguments
        inspect.signature(fn).bind(destination="", amount="", time_ms=0, signature_chain_id="0x1", is_mainnet=True)
    except (TypeError, ValueError):
        return usd_send_typed_data
    return fn


@dataclass(frozen=True)
class UsdcTopupRequest:
    typed_data: dict          # for eth_signTypedData_v4 in the user's wallet
    action: dict              # the /exchange action body (must match the signed message exactly)
    nonce: int                # = action.time (ms)
    amount_micro: int
    source: str               # user's verified master address (expected signer)
    destination: str          # treasury

    def public_view(self) -> dict:
        return {"typed_data": self.typed_data, "action": self.action, "nonce": self.nonce,
                "amount_micro": self.amount_micro, "amount_display": fmt_usd(self.amount_micro)}


def build_topup_request(
    *,
    user_master_address: str,
    amount_micro: int,
    treasury_address: str,
    signature_chain_id: str,
    time_ms: int,
    is_mainnet: bool = True,
    min_topup_micro: int = usd(10),
    max_topup_micro: int | None = None,
) -> UsdcTopupRequest:
    """Typed-data request for a USDC top-up of ``amount_micro`` from the user's master wallet to the treasury."""
    source = _addr(user_master_address, "user")
    dest = _addr(treasury_address, "treasury")
    if dest == source:
        raise ValidationFailed("treasury cannot top up itself")
    if not isinstance(amount_micro, int) or isinstance(amount_micro, bool):
        raise ValidationFailed("amount must be integer micro-USD")
    if amount_micro < min_topup_micro:
        raise ValidationFailed(f"minimum top-up is {fmt_usd(min_topup_micro)}", min_micro=min_topup_micro)
    if max_topup_micro is not None and amount_micro > max_topup_micro:
        raise ValidationFailed(f"maximum top-up is {fmt_usd(max_topup_micro)}", max_micro=max_topup_micro)
    if amount_micro % (MICRO // 100):
        raise ValidationFailed("amount must be whole cents")
    chain = str(signature_chain_id or "").lower()
    if not _CHAIN_ID_RE.fullmatch(chain) or int(chain, 16) == 0:
        raise ValidationFailed("bad signature chain id")
    if not isinstance(time_ms, int) or time_ms <= 0:
        raise ValidationFailed("bad time")
    amount = format_usd_amount(amount_micro)
    typed = _typed_data_builder()(destination=dest, amount=amount, time_ms=time_ms, signature_chain_id=chain,
                                  is_mainnet=is_mainnet)
    action = {
        "type": "usdSend",
        "signatureChainId": chain,
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "destination": dest,
        "amount": amount,
        "time": time_ms,
    }
    return UsdcTopupRequest(typed, action, time_ms, amount_micro, source, dest)


def exchange_body(action: Mapping[str, Any], signature: Mapping[str, Any]) -> dict:
    """Body for POST {HL}/exchange when the API relays the user's signed usdSend. nonce = action.time."""
    if action.get("type") != "usdSend":
        raise ValidationFailed("not a usdSend action")
    r, s, v = signature.get("r"), signature.get("s"), signature.get("v")
    if not (isinstance(r, str) and isinstance(s, str) and isinstance(v, int)):
        raise ValidationFailed("bad signature")
    return {"action": dict(action), "nonce": int(action["time"]), "signature": {"r": r, "s": s, "v": v}}


# --------------------------------------------------------------------------------------------------------------
# Crediting from on-chain detections (hl/deposits.py)
# --------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UsdcDetection:
    tx_hash: str
    from_address: str
    to_address: str
    amount_micro: int
    time_ms: int = 0
    user_id: str | None = None   # set by the scanner when it already resolved the sender
    token: str = "USDC"


def _get(obj: Any, *names: str) -> Any:
    for n in names:
        if isinstance(obj, Mapping) and n in obj:
            return obj[n]
        if not isinstance(obj, Mapping) and hasattr(obj, n):
            return getattr(obj, n)
    return None


def coerce_detection(obj: Any) -> UsdcDetection:
    """Accept a UsdcDetection, a dict, or any object with compatible attributes (hl/deposits.py output).
    Amounts given as Hyperliquid strings are parsed exactly and floored to the micro."""
    if isinstance(obj, UsdcDetection):
        return obj
    amount_micro = _get(obj, "amount_micro")
    if amount_micro is None:
        raw = _get(obj, "amount", "usdc", "usdcValue")
        if raw is None:
            raise ValidationFailed("detection has no amount")
        if isinstance(raw, float):
            raise ValidationFailed("float amount not allowed")
        amount_micro = to_micro(raw if isinstance(raw, (str, int, Decimal)) else str(raw))
    if not isinstance(amount_micro, int) or isinstance(amount_micro, bool):
        raise ValidationFailed("bad detection amount")
    return UsdcDetection(
        tx_hash=str(_get(obj, "tx_hash", "hash") or "").lower(),
        from_address=str(_get(obj, "from_address", "user", "from", "sender") or "").lower(),
        to_address=str(_get(obj, "to_address", "destination", "to") or "").lower(),
        amount_micro=amount_micro,
        time_ms=int(_get(obj, "time_ms", "time") or 0),
        user_id=_get(obj, "user_id"),
        token=str(_get(obj, "token") or "USDC"),
    )


@dataclass
class UsdcOutcome:
    credit: CreditInstruction | None = None
    held: str | None = None        # reason; API posts debit treasury:hl_usdc / credit a suspense account
    ignored: str | None = None
    alerts: list[Alert] = field(default_factory=list)


def credit_from_detection(
    detection: Any,
    *,
    treasury_address: str,
    min_topup_micro: int = usd(10),
    user_for_address: Callable[[str], str | None] | None = None,
) -> UsdcOutcome:
    """Turn one detected USDC transfer into a fee-balance credit.

    * Only transfers TO the treasury count; everything else is ignored.
    * Sender -> user via ``user_for_address`` (verified master wallets, SPEC §4 wallets) unless the scanner
      already set ``user_id``. Unknown sender -> held for manual review (funds arrived; we cannot credit anyone).
    * Below the minimum top-up -> held (the funds are on our treasury; ops credits or returns them by hand).
    * Idempotency key ``usdc_hl:{tx_hash}``; external_ref = tx hash.
    """
    det = coerce_detection(detection)
    out = UsdcOutcome()
    treasury = _addr(treasury_address, "treasury")
    if not _HASH_RE.fullmatch(det.tx_hash):
        raise ValidationFailed("bad transfer hash")
    if det.to_address != treasury:
        out.ignored = "not sent to treasury"
        return out
    if det.from_address == treasury:
        out.ignored = "treasury self-transfer"
        return out
    if det.token.upper() != "USDC":
        out.held = f"unexpected token {det.token[:12]}"
        out.alerts.append(_held_alert(det, out.held, Severity.WARN))
        return out
    if det.amount_micro <= 0:
        out.ignored = "non-positive amount"
        return out
    user_id = det.user_id
    if not user_id and user_for_address is not None and _ADDR_RE.fullmatch(det.from_address):
        user_id = user_for_address(det.from_address)
    if not user_id:
        out.held = "sender is not a verified user wallet"
        out.alerts.append(_held_alert(det, out.held, Severity.WARN if det.amount_micro >= min_topup_micro else Severity.INFO))
        return out
    if det.amount_micro < min_topup_micro:
        out.held = f"below minimum top-up {fmt_usd(min_topup_micro)}"
        out.alerts.append(_held_alert(det, out.held, Severity.WARN))
        out.alerts.append(Alert(kind="topup_held", severity=Severity.INFO, user_id=str(user_id),
                                data={"amount_micro": det.amount_micro, "method": "USDC", "reason": out.held},
                                key=f"topup_held_user:{det.tx_hash}"))
        return out
    out.credit = CreditInstruction(
        user_id=str(user_id),
        amount_micro=det.amount_micro,
        external_ref=det.tx_hash,
        idempotency_key=f"usdc_hl:{det.tx_hash}",
        method="usdc_hl",
        debit_account=TREASURY_HL_USDC,
        credit_account=user_fee_balance_account(str(user_id)),
        kind="deposit",
        memo=f"USDC top-up {det.tx_hash[:10]}…",
        withdrawable=True,
        meta={"from": det.from_address, "time_ms": det.time_ms},
    )
    out.alerts.append(Alert(kind="topup_credited", severity=Severity.INFO, user_id=str(user_id),
                            data={"amount_micro": det.amount_micro, "method": "USDC"}, key=f"topup_credited:{det.tx_hash}"))
    return out


def _held_alert(det: UsdcDetection, reason: str, severity: Severity) -> Alert:
    return Alert(kind="topup_held", severity=severity, user_id=None,
                 data={"amount_micro": det.amount_micro, "method": f"USDC from {det.from_address}", "reason": reason},
                 key=f"topup_held:{det.tx_hash}")
