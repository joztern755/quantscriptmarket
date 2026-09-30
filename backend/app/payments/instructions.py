"""Ledger instructions produced by payment handlers. The API layer turns each into ONE ledger transaction
(idempotent on ``idempotency_key``) and a ``deposits`` row update. Payment modules never touch the DB.

Sign convention (SPEC §4): ledger_entries.amount_micro is +debit / −credit.
  CreditInstruction  -> debit ``debit_account`` (+amount), credit ``credit_account`` (−amount)
  DebitInstruction   -> same shape; for a reversal debit_account is the user's fee balance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = ["CreditInstruction", "DebitInstruction", "user_fee_balance_account", "STRIPE_CLEARING", "TREASURY_HL_USDC"]

STRIPE_CLEARING = "stripe:clearing"
TREASURY_HL_USDC = "treasury:hl_usdc"


def user_fee_balance_account(user_id: str) -> str:
    return f"user:{user_id}:fee_balance"


@dataclass(frozen=True)
class CreditInstruction:
    """Credit a user's fee balance. ``withdrawable`` = whether this money may later leave as USDC.

    Card/wallet (Stripe) credits are NOT withdrawable (spend-only): a stolen card must not be convertible into
    USDC, and returning card money must go back to the card (refund), not to a wallet. USDC credits are.
    """

    user_id: str
    amount_micro: int
    external_ref: str
    idempotency_key: str
    method: str                     # "stripe" | "usdc_hl"
    debit_account: str              # "stripe:clearing" | "treasury:hl_usdc"
    credit_account: str             # "user:{id}:fee_balance"
    kind: str = "deposit"
    memo: str = ""
    withdrawable: bool = False
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.amount_micro, int) or isinstance(self.amount_micro, bool) or self.amount_micro <= 0:
            raise ValueError("amount_micro must be a positive int")
        if not self.user_id or not self.idempotency_key or not self.external_ref:
            raise ValueError("user_id, external_ref and idempotency_key are required")


@dataclass(frozen=True)
class DebitInstruction:
    """Reverse (part of) a credit: debit the user's fee balance, credit the clearing account.

    ``may_go_negative`` is always True for chargebacks/refunds: the money already left us. After posting, the
    API layer MUST re-evaluate billing state (domain/billing.py): a negative balance moves the user's
    subscriptions to past_due -> reduce_only (no new entries, exits allowed)."""

    user_id: str
    amount_micro: int
    external_ref: str
    idempotency_key: str
    method: str
    debit_account: str              # "user:{id}:fee_balance"
    credit_account: str             # "stripe:clearing"
    kind: str                       # "stripe_refund" | "stripe_dispute"
    memo: str = ""
    may_go_negative: bool = True
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.amount_micro, int) or isinstance(self.amount_micro, bool) or self.amount_micro <= 0:
            raise ValueError("amount_micro must be a positive int")
        if not self.user_id or not self.idempotency_key or not self.external_ref:
            raise ValueError("user_id, external_ref and idempotency_key are required")
