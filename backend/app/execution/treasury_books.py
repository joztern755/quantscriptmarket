"""Treasury-side readers for reconciliation (REVIEW_MONEY M7(b), (c), (g)).

``HlBuilderRewardsReader`` (BuilderRewardsReader + BuilderClaimsReader, M7(b))
    Builder rewards on Hyperliquid: still claimable = info ``referral`` → ``builderRewards``; claims = the claiming
    account's non-funding ledger updates of type ``rewardsClaim`` (amount in ``delta.amount``). The request / field
    names are UNVERIFIED (docs blocked where this was written): they live in ``HlRewardsSchema`` and can be changed
    without a code change (env ``HL_REWARDS_REFERRAL_TYPE``, ``HL_REWARDS_UNCLAIMED_FIELD``, ``HL_REWARDS_CLAIM_TYPE``,
    ``HL_REWARDS_CLAIM_AMOUNT_FIELD``, or the same lower-case attributes on Settings). A wrong name only makes
    reconcile report a mismatch; it never moves money on its own (claims are booked 1:1 from what the chain shows).
    Claims are booked into ``treasury:hl_usdc`` (debit) / ``builder:hl_receivable`` (credit) only when the builder
    address IS the treasury address (then the claimed USDC lands in the treasury); otherwise reconcile reports
    ``builder_claims_status = "outside_treasury"`` and books nothing.

``HlTreasuryReader`` (TreasuryReader, M7(g))
    Treasury USDC = perp account value on the validator dex + spot USDC (``spotClearinghouseState`` balances, coin
    ``USDC``, ``total``) + the account value on EACH trusted builder dex (``clearinghouseState`` with ``dex``). Deposits
    to ``destinationDex`` spot / a builder dex are credited by deposits-scan, so all of them are treasury money. The
    treasury holds no positions, so account value = collateral. Bounded: at most ``max_dexes`` builder-dex calls (more
    trusted dexes than that → error, never a silent under-count); every call goes through the shared info client, i.e.
    the shared Hyperliquid rate budget (reconcile runs in the JOBS pool). UNVERIFIED on a live treasury: builder-dex
    collateral is assumed USDC-valued.

``StripeBalanceReader`` (StripeClearingReader, M7(c))
    ``clearing_balance_micro``: Stripe ``/v1/balance`` available + pending in the settlement currency; ``payout_movements``:
    ``/v1/balance_transactions`` of type payout / payout_failure / payout_cancel (bounded pages). The ledger's
    ``stripe:clearing`` is kept in USD, so only a USD settlement currency can be reconciled or booked; any other
    settlement currency (e.g. MYR for a Malaysian account) raises ``StripeCurrencyUnsupported`` and reconcile reports
    ``stripe_clearing_status = "unsupported_currency"`` (booking a MYR payout needs an FX policy — owner decision).
    Known, expected differences the reconcile alert may show: fees absorbed by the platform (``stripe_fee_absorbed``)
    and dispute fees are not in the ledger.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR
from typing import Any, Callable, Iterable, Mapping

from app.errors import ExternalServiceError, ValidationFailed
from app.logging import get_logger
from app.money import parse_decimal, to_micro

from .ports import BuilderClaim, StripePayoutMovement

log = get_logger("app.execution.treasury_books")

__all__ = ["HlRewardsSchema", "HlBuilderRewardsReader", "HlTreasuryReader", "StripeBalanceReader",
           "StripeCurrencyUnsupported", "stripe_settlement_currency"]

DEFAULT_SINCE_MS = 1_704_067_200_000          # 2024-01-01: before our builder code existed
STRIPE_MINOR_TO_MICRO = 10_000                # USD cents → micro-USD


def _conf(settings: Any, name: str, default: str) -> str:
    v = getattr(settings, name, None) if settings is not None else None
    if isinstance(v, str) and v.strip():
        return v.strip()
    return (os.environ.get(name.upper(), "") or default).strip()


# ======================================================================================================== Hyperliquid

@dataclass(frozen=True)
class HlRewardsSchema:
    """Names of the (UNVERIFIED) Hyperliquid builder-rewards fields; configurable."""

    referral_type: str = "referral"
    unclaimed_field: str = "builderRewards"
    claim_type: str = "rewardsClaim"
    claim_amount_field: str = "amount"

    @classmethod
    def from_settings(cls, settings: Any = None) -> "HlRewardsSchema":
        d = cls()
        return cls(referral_type=_conf(settings, "hl_rewards_referral_type", d.referral_type),
                   unclaimed_field=_conf(settings, "hl_rewards_unclaimed_field", d.unclaimed_field),
                   claim_type=_conf(settings, "hl_rewards_claim_type", d.claim_type),
                   claim_amount_field=_conf(settings, "hl_rewards_claim_amount_field", d.claim_amount_field))


def _addr(a: str | None) -> str:
    return (a or "").strip().lower()


class HlBuilderRewardsReader:
    """``BuilderRewardsReader`` + ``BuilderClaimsReader`` for our builder address (see module docstring)."""

    def __init__(self, info: Any, builder_address: str, *, treasury_address: str | None = None,
                 since_ms: int = DEFAULT_SINCE_MS, schema: HlRewardsSchema | None = None, max_pages: int = 20,
                 now_ms: Callable[[], int] | None = None) -> None:
        self.info = info
        self.builder = _addr(builder_address)
        self.treasury = _addr(treasury_address)
        self.since_ms = since_ms
        self.schema = schema or HlRewardsSchema()
        self.max_pages = max_pages
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    @property
    def claims_into_treasury(self) -> bool:
        """Claimed rewards land in the account that claims them (the builder); they are treasury money only when the
        builder address is the treasury address."""
        return bool(self.builder) and self.builder == self.treasury

    def _require(self) -> None:
        if not self.builder:
            raise ValidationFailed("builder address not configured")

    def unclaimed_builder_rewards_micro(self) -> int:
        self._require()
        ref = self.info.post({"type": self.schema.referral_type, "user": self.builder})
        if ref is not None and not isinstance(ref, Mapping):
            raise ExternalServiceError("unexpected referral shape")
        raw = (ref or {}).get(self.schema.unclaimed_field, "0")
        return to_micro(parse_decimal(str(raw if raw is not None else "0")), ROUND_FLOOR)

    def builder_reward_claims(self, since_ms: int) -> list[BuilderClaim]:
        self._require()
        out: list[BuilderClaim] = []
        for u in self.info.iter_user_non_funding_ledger_updates(self.builder, int(since_ms), self._now_ms(),
                                                                max_pages=self.max_pages):
            delta = u.get("delta") or {}
            if not isinstance(delta, Mapping) or delta.get("type") != self.schema.claim_type:
                continue
            t = u.get("time")
            if isinstance(t, bool) or not isinstance(t, int):
                raise ExternalServiceError("builder rewards claim without integer time")
            amount = to_micro(parse_decimal(str(delta.get(self.schema.claim_amount_field, "0"))), ROUND_FLOOR)
            if amount <= 0:
                continue
            h = str(u.get("hash") or "").lower()
            if not h.startswith("0x") or not h[2:].isalnum() or set(h[2:]) <= {"0"}:
                h = "nohash"                       # system actions may carry an all-zero hash: time + amount identify
            ref = f"{h}:{t}" if h != "nohash" else f"nohash:{t}:{amount}"
            out.append(BuilderClaim(ref=ref, time_ms=t, amount_micro=amount))
        out.sort(key=lambda c: (c.time_ms, c.ref))
        return out

    def cumulative_builder_rewards_micro(self) -> int:
        """Accrued on-chain, all time = still claimable + everything claimed since ``since_ms``."""
        return self.unclaimed_builder_rewards_micro() + sum(c.amount_micro for c in self.builder_reward_claims(self.since_ms))


class HlTreasuryReader:
    """``TreasuryReader`` (M7(g)): perp (validator dex) + spot USDC + each trusted builder dex, for the treasury."""

    def __init__(self, info: Any, treasury_address: str, *,
                 dexes: Callable[[], Iterable[str]] | None = None, max_dexes: int = 16,
                 include_spot: bool = True) -> None:
        self.info = info
        self.treasury = _addr(treasury_address)
        self._dexes = dexes
        self.max_dexes = max_dexes
        self.include_spot = include_spot
        self.last_breakdown: dict[str, int] = {}

    def _builder_dexes(self) -> list[str]:
        if self._dexes is None:
            return []
        names = sorted({str(d) for d in self._dexes() if d})          # "" (validator dex) is read separately
        if len(names) > self.max_dexes:
            raise ValidationFailed("more trusted builder dexes than the treasury reader may query",
                                   dexes=len(names), max_dexes=self.max_dexes)
        return names

    def _account_value(self, dex: str) -> int:
        state = self.info.clearinghouse_state(self.treasury, dex)
        value = ((state or {}).get("marginSummary") or {}).get("accountValue", "0")
        return to_micro(parse_decimal(str(value)), ROUND_FLOOR)

    def _spot_usdc(self) -> int:
        out = self.info.post({"type": "spotClearinghouseState", "user": self.treasury})
        if not isinstance(out, Mapping) or not isinstance(out.get("balances", []), list):
            raise ExternalServiceError("unexpected spotClearinghouseState shape")
        total = 0
        for b in out.get("balances") or []:
            if isinstance(b, Mapping) and str(b.get("coin", "")).upper() == "USDC":
                total += to_micro(parse_decimal(str(b.get("total", "0"))), ROUND_FLOOR)
        return total

    def breakdown(self) -> dict[str, int]:
        if not self.treasury:
            raise ValidationFailed("treasury address not configured")
        dexes = self._builder_dexes()                                # validated BEFORE any call (bounded)
        parts: dict[str, int] = {"perp": self._account_value("")}
        if self.include_spot:
            parts["spot_usdc"] = self._spot_usdc()
        for d in dexes:
            parts[f"dex:{d}"] = self._account_value(d)
        self.last_breakdown = dict(parts)
        return parts

    def treasury_usdc_micro(self) -> int:
        return sum(self.breakdown().values())


# ======================================================================================================== Stripe

class StripeCurrencyUnsupported(ValidationFailed):
    """The Stripe settlement currency is not USD: the USD ledger cannot be reconciled / booked without an FX policy."""

    code = "stripe_currency_unsupported"


def stripe_settlement_currency(settings: Any = None) -> str:
    return _conf(settings, "stripe_settlement_currency", "usd").lower()


PAYOUT_TYPES = ("payout", "payout_failure", "payout_cancel")


class StripeBalanceReader:
    """``StripeClearingReader`` over a Stripe gateway exposing ``retrieve_balance()`` and
    ``list_balance_transactions(params) -> {"data": [...], "has_more": bool}`` (``app.payments.stripe_pay``)."""

    def __init__(self, gateway: Any, *, currency: str = "usd", max_pages: int = 10, page_size: int = 100) -> None:
        self.gateway = gateway
        self.currency = (currency or "usd").lower()
        self.max_pages = max_pages
        self.page_size = max(1, min(int(page_size), 100))

    def _require_usd(self) -> None:
        if self.currency != "usd":
            raise StripeCurrencyUnsupported("Stripe settlement currency is not USD", currency=self.currency)

    @staticmethod
    def _int(v: Any, what: str) -> int:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ExternalServiceError(f"stripe {what} is not an integer")
        return v

    def clearing_balance_micro(self) -> int:
        self._require_usd()
        bal = self.gateway.retrieve_balance()
        if not isinstance(bal, Mapping):
            raise ExternalServiceError("unexpected stripe balance shape")
        total = 0
        for bucket in ("available", "pending"):
            for e in bal.get(bucket) or []:
                if not isinstance(e, Mapping):
                    raise ExternalServiceError("unexpected stripe balance entry")
                cur = str(e.get("currency", "")).lower()
                amount = self._int(e.get("amount"), "balance amount")
                if cur == self.currency:
                    total += amount
                elif amount:
                    raise StripeCurrencyUnsupported("Stripe balance holds another currency", currency=cur)
        return total * STRIPE_MINOR_TO_MICRO

    def payout_movements(self, since_ms: int) -> list[StripePayoutMovement]:
        self._require_usd()
        out: list[StripePayoutMovement] = []
        since_s = max(0, int(since_ms) // 1000)
        for typ in PAYOUT_TYPES:
            after: str | None = None
            for _ in range(self.max_pages):
                params: dict[str, Any] = {"type": typ, "created[gte]": since_s, "limit": self.page_size}
                if after:
                    params["starting_after"] = after
                page = self.gateway.list_balance_transactions(params)
                data = (page or {}).get("data") if isinstance(page, Mapping) else None
                if not isinstance(data, list):
                    raise ExternalServiceError("unexpected stripe balance_transactions shape")
                for bt in data:
                    mv = self._movement(bt, typ)
                    if mv is not None:
                        out.append(mv)
                if not page.get("has_more") or not data:
                    break
                after = str(data[-1].get("id", "")) or None
                if after is None:
                    break
            else:
                raise ExternalServiceError("stripe balance_transactions pagination exceeded", type=typ)
        out.sort(key=lambda m: (m.created_ms, m.txn_id))
        return out

    def _movement(self, bt: Any, typ: str) -> StripePayoutMovement | None:
        if not isinstance(bt, Mapping) or bt.get("type") != typ:
            raise ExternalServiceError("unexpected stripe balance transaction")
        txn = str(bt.get("id", ""))
        if not txn.startswith("txn_") or not txn[4:].isalnum():
            raise ExternalServiceError("stripe balance transaction without a txn_ id")
        cur = str(bt.get("currency", "")).lower()
        if cur != self.currency:
            raise StripeCurrencyUnsupported("Stripe payout in another currency", currency=cur, txn=txn)
        amount = self._int(bt.get("amount"), "amount")
        fee = self._int(bt.get("fee", 0), "fee")
        created = self._int(bt.get("created"), "created")
        source = bt.get("source")
        payout_id = str(source.get("id") if isinstance(source, Mapping) else source or "")
        if typ == "payout":
            if amount >= 0 or fee < 0:
                raise ExternalServiceError("stripe payout balance transaction with unexpected sign", txn=txn)
            return StripePayoutMovement(txn_id=txn, payout_id=payout_id, created_ms=created * 1000,
                                        amount_micro=-amount * STRIPE_MINOR_TO_MICRO,
                                        fee_micro=fee * STRIPE_MINOR_TO_MICRO)
        if amount <= 0:
            raise ExternalServiceError("stripe payout reversal with unexpected sign", txn=txn)
        return StripePayoutMovement(txn_id=txn, payout_id=payout_id, created_ms=created * 1000,
                                    amount_micro=amount * STRIPE_MINOR_TO_MICRO, fee_micro=max(0, fee) * STRIPE_MINOR_TO_MICRO,
                                    reversal=True)


def stripe_reader_from_settings(settings: Any, gateway_factory: Callable[[Any], Any] | None = None
                                ) -> StripeBalanceReader | None:
    """The reconcile Stripe reader, or None (→ ``not_configured``) when no Stripe secret key is configured."""
    if not getattr(settings, "stripe_secret_key", ""):
        return None
    if gateway_factory is None:
        from app.payments.stripe_pay import default_gateway as gateway_factory  # noqa: N813
    return StripeBalanceReader(gateway_factory(settings), currency=stripe_settlement_currency(settings))


__all__ += ["stripe_reader_from_settings", "PAYOUT_TYPES"]
