"""Detect USDC transfers into the treasury from ``userNonFundingLedgerUpdates(treasury)`` (SPEC §0, §8).

Ledger delta types observed on mainnet (2026-09-30; fixture ``userNonFundingLedgerUpdates_sample.json``):
- ``send`` (current, since ~Nov 2025): {user (sender), destination, sourceDex, destinationDex, token, amount,
  usdcValue, fee, nativeTokenFee, nonce, feeToken}. ``sourceDex``/``destinationDex``: "" = validator-perp
  balance, "spot", or a builder dex ("xyz"). The same type covers self-transfers between balances
  (user == destination) and spot tokens (token ≠ USDC). ``nonce`` equals the signed action's nonce/time, so a
  ``usdSend`` we prepared (``time`` = T) shows up with ``nonce`` = T — used to match a confirm request.
- ``internalTransfer`` (legacy, seen Aug 2025): {usdc, user, destination, fee}.
- ``spotTransfer``: {token, amount, usdcValue, user, destination, fee, nativeTokenFee, nonce, feeToken}.
- ``usdSend``: NOT seen in any sampled history (UNVERIFIED name); parsed defensively like internalTransfer.
- ``deposit`` (bridge from Arbitrum: {usdc} only — no sender) → reported as unattributable, never credited.
- Others (withdraw, accountClassTransfer, subAccountTransfer, vaultDeposit/Withdraw, cStakingTransfer,
  spotGenesis, gossipPriorityGasAuction, …) are ignored.

Credit rule (documented contract; UNCERTAIN where marked):
- Only token USDC, destination == treasury, sender ≠ treasury, non-zero tx hash (the idempotency key).
- ``amount_micro`` = floor(amount − fee) when fee is charged in USDC. Whether a send fee (seen: 1 USDC when the
  destination account is new) is deducted from the amount or charged on top to the sender is UNVERIFIED, so we
  credit the conservative figure and set ``fee_uncertain``; our treasury is an existing account, where every
  observed fee was 0.
- A transfer from an address that is not a verified user wallet is returned in ``unverified`` (funds arrived;
  ``app.payments.usdc.credit_from_detection`` holds it for manual review).
- Any destination balance ("" / "spot" / builder dex) counts — it is our treasury's money either way; the dex is
  recorded for reconciliation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Collection, Iterable, Mapping

from app.errors import ValidationFailed
from app.money import parse_decimal, to_micro

__all__ = ["DepositDetected", "DepositScan", "detect_deposits", "TRANSFER_TYPES"]

TRANSFER_TYPES = ("send", "usdSend", "internalTransfer", "spotTransfer")
_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_ZERO_HASH = "0x" + "0" * 64


@dataclass(frozen=True)
class DepositDetected:
    user_address: str         # sender (lower-case)
    amount_micro: int         # conservative credit: floor(amount − USDC fee)
    hash: str                 # idempotency key (lower-case tx hash)
    time: int                 # ms
    treasury_address: str
    gross_micro: int          # floor(amount) as reported
    fee_micro: int            # USDC fee reported on the transfer (0 normally)
    kind: str                 # ledger delta type
    token: str = "USDC"
    source_dex: str | None = None
    destination_dex: str | None = None
    nonce: int | None = None
    fee_uncertain: bool = False

    # --- aliases read by app.payments.usdc.coerce_detection -------------------------------------------------
    @property
    def tx_hash(self) -> str:
        return self.hash

    @property
    def from_address(self) -> str:
        return self.user_address

    @property
    def to_address(self) -> str:
        return self.treasury_address

    @property
    def time_ms(self) -> int:
        return self.time

    @property
    def idempotency_key(self) -> str:
        return f"usdc_hl:{self.hash}"


@dataclass
class DepositScan:
    deposits: list[DepositDetected] = field(default_factory=list)      # from verified user wallets → credit
    unverified: list[DepositDetected] = field(default_factory=list)    # USDC in from unknown senders → hold
    unattributable: list[Mapping[str, Any]] = field(default_factory=list)  # e.g. bridge deposits (no sender)
    skipped: list[tuple[Mapping[str, Any], str]] = field(default_factory=list)


def _addr(v: Any) -> str | None:
    s = str(v or "").lower()
    return s if _ADDR_RE.fullmatch(s) else None


def detect_deposits(ledger_updates: Iterable[Mapping[str, Any]], *, treasury_address: str,
                    verified_wallets: Collection[str] | None = None,
                    since_ms: int | None = None) -> DepositScan:
    """Classify the treasury's ledger updates. ``verified_wallets=None`` accepts every sender (the caller then
    resolves users itself). Duplicate hashes within one scan are reported and credited once."""
    treasury = _addr(treasury_address)
    if treasury is None:
        raise ValidationFailed("bad treasury address")
    verified = None if verified_wallets is None else {w.lower() for w in verified_wallets}
    out = DepositScan()
    seen: set[str] = set()
    for raw in ledger_updates:
        delta = raw.get("delta") if isinstance(raw, Mapping) else None
        kind = (delta or {}).get("type")
        t = raw.get("time") if isinstance(raw, Mapping) else None
        if not isinstance(delta, Mapping) or not isinstance(t, int):
            out.skipped.append((raw, "malformed"))
            continue
        if since_ms is not None and t < since_ms:
            continue
        if kind == "deposit":
            out.unattributable.append(raw)
            continue
        if kind not in TRANSFER_TYPES:
            continue
        dest, src = _addr(delta.get("destination")), _addr(delta.get("user"))
        if dest != treasury:
            continue  # outbound or unrelated
        if src is None or src == treasury:
            out.skipped.append((raw, "self-transfer or no sender"))
            continue
        token = str(delta.get("token") or "USDC")
        if token != "USDC":
            out.skipped.append((raw, f"token {token} is not USDC"))
            continue
        h = str(raw.get("hash") or "").lower()
        if not _HASH_RE.fullmatch(h) or h == _ZERO_HASH:
            out.skipped.append((raw, "no usable tx hash (cannot be idempotent)"))
            continue
        if h in seen:
            out.skipped.append((raw, "duplicate hash in scan"))
            continue
        try:
            amount = parse_decimal(delta["amount"] if "amount" in delta else delta["usdc"])
            fee = parse_decimal(delta.get("fee") or "0")
        except (KeyError, TypeError, ValueError):
            out.skipped.append((raw, "unparseable amount"))
            continue
        fee_token = str(delta.get("feeToken") or "USDC") if kind in ("send", "spotTransfer") else "USDC"
        usdc_fee = fee if fee_token == "USDC" else Decimal(0)
        if amount <= 0 or usdc_fee < 0:
            out.skipped.append((raw, "non-positive amount"))
            continue
        seen.add(h)
        nonce = delta.get("nonce")
        det = DepositDetected(
            user_address=src, amount_micro=max(0, to_micro(amount - usdc_fee, ROUND_FLOOR)), hash=h, time=t,
            treasury_address=treasury, gross_micro=to_micro(amount, ROUND_FLOOR),
            fee_micro=to_micro(usdc_fee, ROUND_FLOOR), kind=str(kind), token=token,
            source_dex=delta.get("sourceDex"), destination_dex=delta.get("destinationDex"),
            nonce=nonce if isinstance(nonce, int) and not isinstance(nonce, bool) else None,
            fee_uncertain=usdc_fee > 0,
        )
        if verified is None or src in verified:
            out.deposits.append(det)
        else:
            out.unverified.append(det)
    return out
