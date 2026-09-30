"""Stripe top-ups of the prepaid fee balance (SPEC §0, §1 "Fee balance", §8 POST /deposits/stripe, POST /webhooks/stripe).

Flow
  1. ``create_topup_intent`` (API: POST /v1/deposits/stripe) creates a PaymentIntent with
     ``automatic_payment_methods={"enabled": True}``. The Payment Element then shows every method enabled in the
     Stripe Dashboard that fits the currency/amount/device: cards, Apple Pay, Google Pay, Link and, for MYR,
     Malaysian local methods (FPX online banking, GrabPay). Only ``client_secret`` goes to the browser.
  2. The browser confirms the payment with Stripe.js. **Client-side "success" is never trusted** — nothing is
     credited by the API on the client's word.
  3. Stripe calls POST /v1/webhooks/stripe. ``verify_webhook`` checks the signature on the RAW body;
     ``handle_event`` re-fetches the PaymentIntent (with ``latest_charge.balance_transaction``) and turns it into
     a ``CreditInstruction`` (idempotency key ``stripe:{pi_id}``) for gross − actual Stripe fee (SPEC §1,
     owner 30 Sep 2026: fee passed to the user). The API layer posts it as one ledger tx: debit
     ``stripe:clearing``, credit ``user:{id}:fee_balance`` (both NET — the fee never reaches our books; Stripe
     keeps it). Refunds and disputes produce ``DebitInstruction``s + critical ops alerts.

Currency decision (fee balance is USD; ledger is single-currency micro-USD)
  * DEFAULT: charge **USD**. Gross credit = amount received (cents × 10_000 micro), no FX risk, no rate to
    store. Cards, Apple Pay and Google Pay all work in USD. A Malaysian Stripe account settles in MYR, so the
    balance transaction is in MYR; the fee is applied as a SHARE of the gross (``bt.fee / bt.amount``), which
    needs no FX rate. Any currency-conversion fee Stripe itemizes on the charge is therefore passed through
    too. Malaysian cardholders may also see a foreign-currency fee from their bank — disclose on the top-up screen.
  * FPX and GrabPay are **MYR-only**, so they never appear for a USD PaymentIntent. To offer them, the MYR path
    (behind ``StripeTopupConfig.myr_enabled``, default OFF) charges MYR for a USD credit using a rate that WE
    quote and lock at intent creation: ``charge_myr = credit_usd × mid_rate × (1 + spread_bps)`` rounded up to
    the sen; mid rate, spread, source, quote id and the exact gross ``credit_micro`` are stored in the
    PaymentIntent metadata, so the webhook credits exactly ``credit_micro`` − fee (pro-rata if less was
    received) and refunds / disputes reverse pro-rata of that same locked amount. PaymentIntents do not return an FX quote themselves;
    rate sources (inject via ``FxRateProvider``): Stripe's FX Quotes API (``/v1/fx_quotes``, preview when last
    checked — verify availability for a MY account), Bank Negara Malaysia's public exchange-rate API
    (api.bnm.gov.my, business-day reference rates — needs a staleness rule), or a commercial feed. The spread
    covers Stripe's conversion fee and intraday moves; the platform bears residual FX risk until settlement.
  * Rejected: a multi-currency fee balance (every fee rule in SPEC §1 is USD; would double the ledger surface).

Stripe account restriction (FLAG FOR OWNER): Stripe's restricted-business list covers crypto / virtual-currency,
trading and investment-adjacent services. A prepaid fee balance for a Hyperliquid strategy marketplace may need
explicit Stripe approval (describe it accurately: SaaS subscriptions + platform fees, no custody of trading
funds, no crypto sold). Do not go live on Stripe before the account is approved for this use; an unapproved
account can be frozen with funds held. Card-funded credits are spend-only (``withdrawable=False``), which also
keeps the fee balance closed-loop — confirm with Malaysian counsel that a closed-loop prepaid balance is outside
BNM e-money issuer rules.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.alerts.notifier import Alert, Severity
from app.errors import AppError, Conflict, ExternalServiceError, Forbidden, ValidationFailed
from app.logging import get_logger
from app.money import MICRO, apply_bps_floor, fmt_usd, usd
from app.payments.instructions import (
    STRIPE_CLEARING,
    CreditInstruction,
    DebitInstruction,
    user_fee_balance_account,
)

log = get_logger("app.payments.stripe")

__all__ = [
    "PURPOSE_TOPUP",
    "StripeTopupConfig",
    "FxQuote",
    "FxRateProvider",
    "StripeGateway",
    "StripeHttpGateway",
    "StripeLibGateway",
    "default_gateway",
    "TopupIntent",
    "create_topup_intent",
    "stripe_fee_micro",
    "make_fee_lookup",
    "StripeFeeNotReady",
    "FEE_EXPAND",
    "WebhookVerificationError",
    "verify_webhook",
    "compute_signature",
    "DepositRecord",
    "WebhookOutcome",
    "handle_event",
    "HANDLED_EVENTS",
]

PURPOSE_TOPUP = "fee_balance_topup"
USD_CENT_MICRO = MICRO // 100  # 10_000 micro per cent
SUPPORTED_CURRENCIES = ("usd", "myr")
HANDLED_EVENTS = (
    "payment_intent.succeeded",
    "payment_intent.payment_failed",
    "charge.refunded",
    "charge.dispute.created",
    "charge.dispute.closed",
)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# ==============================================================================================================
# Config
# ==============================================================================================================
@dataclass(frozen=True)
class StripeTopupConfig:
    """Stripe top-up settings. Built from ``app.config.Settings`` via ``from_settings``; the optional fields are
    read with getattr so config.py can add them later without breaking this module:
    ``stripe_max_topup_micro``, ``feature_stripe_myr``, ``stripe_myr_fx_spread_bps``, ``stripe_api_version``."""

    min_topup_micro: int = usd(10)
    max_topup_micro: int = usd(10_000)
    myr_enabled: bool = False
    myr_fx_spread_bps: int = 150
    fx_quote_max_age_s: int = 60
    fx_sanity_bounds: Mapping[str, tuple[Decimal, Decimal]] = field(
        default_factory=lambda: {"myr": (Decimal("2.5"), Decimal("8"))}  # MYR per USD; outside -> refuse (broken feed)
    )
    stripe_fee_absorbed: bool = False      # SPEC §1 (owner 30 Sep 2026): pass-through, credit net of actual fee
    # Pre-payment fee ESTIMATE shown to the user (Terms 10.3). [CONFIRM] from the account's Stripe pricing page;
    # None = no estimate (UI must then say "processor fee deducted; exact amount shown after payment").
    fee_estimate_bps: int | None = None
    fee_estimate_fixed_micro: int = 0
    env: str = "dev"
    livemode_required: bool | None = None  # prod: True (reject test-mode events); None = don't check
    api_version: str | None = None         # pin to the webhook endpoint's API version in prod

    @classmethod
    def from_settings(cls, settings: Any) -> "StripeTopupConfig":
        econ = settings.economics
        return cls(
            min_topup_micro=econ.min_topup_micro,
            max_topup_micro=getattr(settings, "stripe_max_topup_micro", usd(10_000)),
            myr_enabled=bool(getattr(settings, "feature_stripe_myr", False)),
            myr_fx_spread_bps=int(getattr(settings, "stripe_myr_fx_spread_bps", 150)),
            stripe_fee_absorbed=econ.stripe_fee_absorbed,
            fee_estimate_bps=getattr(settings, "stripe_fee_estimate_bps", None),
            fee_estimate_fixed_micro=int(getattr(settings, "stripe_fee_estimate_fixed_micro", 0) or 0),
            env=settings.env,
            livemode_required=True if settings.is_prod else None,
            api_version=getattr(settings, "stripe_api_version", None) or None,
        )


# ==============================================================================================================
# FX (MYR path only)
# ==============================================================================================================
@dataclass(frozen=True)
class FxQuote:
    currency: str      # quote currency, lower-case ("myr")
    rate: Decimal      # units of ``currency`` per 1 USD (mid)
    source: str        # e.g. "stripe_fx_quotes", "bnm"
    quote_id: str
    quoted_at: float   # epoch seconds


class FxRateProvider(Protocol):
    def quote_usd_to(self, currency: str) -> FxQuote: ...


# ==============================================================================================================
# Gateway (Stripe API access)
# ==============================================================================================================
class StripeGateway(Protocol):
    def create_payment_intent(self, params: dict, idempotency_key: str) -> dict: ...

    def retrieve_payment_intent(self, pi_id: str, expand: Sequence[str] = ()) -> dict: ...


FEE_EXPAND = ("latest_charge.balance_transaction",)


class StripeFeeNotReady(ExternalServiceError):
    """The charge's balance_transaction is not available yet. The webhook must answer non-2xx so Stripe retries
    (it retries with backoff for up to 3 days); nothing is credited until the actual fee is known."""

    code = "stripe_fee_not_ready"


def stripe_fee_micro(pi: Mapping[str, Any], gross_micro: int) -> int:
    """Actual Stripe fee for a PaymentIntent, in micro-USD, from ``latest_charge.balance_transaction`` (SPEC §1:
    credit = amount received − actual Stripe fee).

    The balance transaction is in the account's SETTLEMENT currency (MYR for a Malaysian account) while the credit
    is USD, so the fee is applied as a share of the gross: ``fee = gross_micro × bt.fee / bt.amount`` (floored —
    fees charged to users round down, SPEC §1). This is currency-agnostic and needs no FX rate. ``bt.fee``
    includes every Stripe fee line on the charge (processing, and any currency-conversion fee Stripe itemizes).
    """
    charge = pi.get("latest_charge")
    if not isinstance(charge, Mapping):
        raise StripeFeeNotReady("latest_charge not expanded/available")
    bt = charge.get("balance_transaction")
    if not isinstance(bt, Mapping):
        raise StripeFeeNotReady("balance_transaction not available yet")
    amount, fee = bt.get("amount"), bt.get("fee")
    if isinstance(amount, bool) or isinstance(fee, bool) or not isinstance(amount, int) or not isinstance(fee, int):
        raise ValidationFailed("balance_transaction amount/fee not integers")
    if amount <= 0 or fee < 0 or fee >= amount:
        raise ValidationFailed("balance_transaction amount/fee out of range")
    return gross_micro * fee // amount


def _has_balance_transaction(pi: Mapping[str, Any]) -> bool:
    charge = pi.get("latest_charge")
    return isinstance(charge, Mapping) and isinstance(charge.get("balance_transaction"), Mapping)


def _gross_micro_of(pi: Mapping[str, Any]) -> int:
    """Gross USD value (micro) of what a succeeded top-up PaymentIntent actually received — the same figure
    ``handle_event`` credits before the fee (USD: cents × 10_000; MYR: pro-rata of the locked ``credit_micro``)."""
    meta = pi.get("metadata") or {}
    currency = str(pi.get("currency", "")).lower()
    amount = _int(pi.get("amount"), "amount")
    received = _int(pi.get("amount_received"), "amount_received")
    record = _record_from_meta(meta, str(pi.get("id", "")), currency, amount)
    if record is None:
        raise ValidationFailed("not a complete fee-balance top-up PaymentIntent")
    if received <= 0:
        raise ValidationFailed("amount_received is zero")
    return _credit_for(record, received)


def make_fee_lookup(gateway: StripeGateway | None) -> Callable[[dict], int]:
    """Fee port for ``handle_event(..., fee_lookup=)`` (API: ``StripeAdapter.handle_event``).

    Returns ``lookup(pi) -> fee_micro``: the ACTUAL Stripe fee for the PaymentIntent, read from its charge's
    ``balance_transaction`` and applied as a share of the gross credit (``stripe_fee_micro``). If the PaymentIntent
    it is given was not expanded (a raw webhook object has ``latest_charge`` as an id string), it re-fetches it
    through ``gateway`` with ``FEE_EXPAND``. Still no balance transaction → ``StripeFeeNotReady`` (webhook answers
    non-2xx, Stripe redelivers later; nothing is credited meanwhile). Malformed data → ``ValidationFailed`` (the
    handler turns it into a manual-review alert, never a guessed credit).
    """

    def lookup(pi: dict) -> int:
        pi_id = str(pi.get("id", ""))
        src: Mapping[str, Any] = pi
        if not _has_balance_transaction(src):
            if gateway is None:
                raise StripeFeeNotReady("balance_transaction not expanded and no gateway to fetch it")
            src = gateway.retrieve_payment_intent(pi_id, expand=FEE_EXPAND)
            if not isinstance(src, Mapping) or src.get("id") != pi_id:
                raise ValidationFailed("re-fetched PaymentIntent id mismatch")
        return stripe_fee_micro(src, _gross_micro_of(src))

    return lookup


def _form_encode(params: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe form encoding: nested dicts -> a[b]=..., lists -> a[0]=..., bools -> 'true'/'false'."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else str(k)
        if v is None:
            continue
        if isinstance(v, Mapping):
            out.extend(_form_encode(v, key))
        elif isinstance(v, (list, tuple)):
            for i, item in enumerate(v):
                if isinstance(item, Mapping):
                    out.extend(_form_encode(item, f"{key}[{i}]"))
                else:
                    out.append((f"{key}[{i}]", _scalar(item)))
        else:
            out.append((key, _scalar(v)))
    return out


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class StripeHttpGateway:
    """Minimal Stripe REST client over ``requests`` (no stripe lib needed). Never logs the secret key."""

    def __init__(self, secret_key: str, session: Any | None = None, api_base: str = "https://api.stripe.com",
                 api_version: str | None = None, timeout: float = 20.0) -> None:
        if not secret_key:
            raise ValidationFailed("stripe secret key not configured")
        self._key = secret_key
        self._session = session
        self.api_base = api_base.rstrip("/")
        self.api_version = api_version
        self.timeout = timeout

    def _headers(self, idempotency_key: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._key}"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        if self.api_version:
            h["Stripe-Version"] = self.api_version
        return h

    def _session_(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _handle(self, resp: Any, what: str) -> dict:
        status = getattr(resp, "status_code", 0)
        try:
            body = resp.json()
        except Exception:
            body = {}
        if 200 <= status < 300 and isinstance(body, dict):
            return body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        etype, ecode = err.get("type", ""), err.get("code", "")
        if etype == "idempotency_error":
            raise Conflict("idempotency key reused with different parameters", stripe_code=ecode)
        if status == 429 or status >= 500 or status == 0:
            raise ExternalServiceError(f"stripe {what} unavailable", status=status)
        raise ValidationFailed(f"stripe {what} rejected", status=status, stripe_type=etype, stripe_code=ecode)

    def create_payment_intent(self, params: dict, idempotency_key: str) -> dict:
        try:
            resp = self._session_().post(f"{self.api_base}/v1/payment_intents", data=_form_encode(params),
                                         headers=self._headers(idempotency_key), timeout=self.timeout)
        except Exception as e:
            raise ExternalServiceError("stripe unreachable", error=type(e).__name__) from None
        return self._handle(resp, "create_payment_intent")

    def retrieve_payment_intent(self, pi_id: str, expand: Sequence[str] = ()) -> dict:
        if not re.fullmatch(r"pi_[A-Za-z0-9]+", pi_id or ""):
            raise ValidationFailed("bad payment intent id")
        params = [("expand[]", e) for e in expand]
        try:
            resp = self._session_().get(f"{self.api_base}/v1/payment_intents/{pi_id}", params=params,
                                        headers=self._headers(), timeout=self.timeout)
        except Exception as e:
            raise ExternalServiceError("stripe unreachable", error=type(e).__name__) from None
        return self._handle(resp, "retrieve_payment_intent")


def _stripe_lib():
    try:
        import stripe  # type: ignore

        return stripe
    except Exception:
        return None


def _plain(obj: Any) -> dict:
    for attr in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return json.loads(json.dumps(fn(), default=str))
            except Exception:
                pass
    return json.loads(str(obj))


class StripeLibGateway:
    """Uses the official ``stripe`` library (prod dependency). Not exercised in this repo's offline tests."""

    def __init__(self, secret_key: str, api_version: str | None = None) -> None:
        lib = _stripe_lib()
        if lib is None:
            raise RuntimeError("stripe library not installed")
        if not secret_key:
            raise ValidationFailed("stripe secret key not configured")
        self._lib, self._key, self.api_version = lib, secret_key, api_version

    def _opts(self) -> dict:
        o: dict = {"api_key": self._key}
        if self.api_version:
            o["stripe_version"] = self.api_version
        return o

    def create_payment_intent(self, params: dict, idempotency_key: str) -> dict:
        try:
            pi = self._lib.PaymentIntent.create(idempotency_key=idempotency_key, **self._opts(), **params)
        except Exception as e:
            if type(e).__name__ == "IdempotencyError":
                raise Conflict("idempotency key reused with different parameters") from None
            raise ExternalServiceError("stripe create_payment_intent failed", error=type(e).__name__) from None
        return _plain(pi)

    def retrieve_payment_intent(self, pi_id: str, expand: Sequence[str] = ()) -> dict:
        try:
            kw = dict(self._opts())
            if expand:
                kw["expand"] = list(expand)
            return _plain(self._lib.PaymentIntent.retrieve(pi_id, **kw))
        except Exception as e:
            raise ExternalServiceError("stripe retrieve_payment_intent failed", error=type(e).__name__) from None


def default_gateway(settings: Any) -> StripeGateway:
    version = getattr(settings, "stripe_api_version", None) or None
    if _stripe_lib() is not None:
        return StripeLibGateway(settings.stripe_secret_key, api_version=version)
    return StripeHttpGateway(settings.stripe_secret_key, api_version=version)


# ==============================================================================================================
# Create top-up PaymentIntent
# ==============================================================================================================
@dataclass(frozen=True)
class TopupIntent:
    payment_intent_id: str
    client_secret: str          # to the browser only; never log
    credit_micro: int           # GROSS USD value of the payment; the credit is this minus the actual Stripe fee
    currency: str               # presentment currency charged ("usd" | "myr")
    amount_minor: int           # amount charged in ``currency`` minor units (cents / sen)
    idempotency_key: str
    fx_rate_applied: str | None = None   # MYR per USD incl. spread (MYR path)
    fx_quote: FxQuote | None = None
    fee_passthrough: bool = True
    estimated_fee_micro: int | None = None

    def public_view(self) -> dict:
        """What the API returns to the browser."""
        d = {
            "payment_intent_id": self.payment_intent_id,
            "client_secret": self.client_secret,
            "credit_micro": self.credit_micro,
            "credit_display": fmt_usd(self.credit_micro),
            "currency": self.currency,
            "amount_minor": self.amount_minor,
            "fee_passthrough": self.fee_passthrough,
        }
        if self.fee_passthrough:
            d["fee_note"] = "The payment processor's fee is deducted from the amount credited."
            if self.estimated_fee_micro is not None:
                d["estimated_fee_micro"] = self.estimated_fee_micro
                d["estimated_credit_micro"] = self.credit_micro - self.estimated_fee_micro
        if self.fx_rate_applied:
            d["fx_rate_applied"] = self.fx_rate_applied
        return d


def _user_id_of(user: Any) -> str:
    if isinstance(user, str):
        uid = user
    elif isinstance(user, Mapping):
        uid = str(user.get("id") or "")
        status = user.get("status")
        if status and status != "active":
            raise Forbidden("account not active")
    else:
        uid = str(getattr(user, "id", "") or "")
        status = getattr(user, "status", None)
        if status and status != "active":
            raise Forbidden("account not active")
    if not uid or len(uid) > 64 or not re.fullmatch(r"[A-Za-z0-9_\-]+", uid):
        raise ValidationFailed("bad user id")
    return uid


def _myr_minor_for(credit_micro: int, mid_rate: Decimal, spread_bps: int) -> tuple[int, Decimal]:
    applied = mid_rate * (Decimal(10_000 + spread_bps) / Decimal(10_000))
    myr = Decimal(credit_micro) / Decimal(MICRO) * applied
    minor = int((myr * 100).to_integral_value(rounding=ROUND_CEILING))
    return minor, applied


def create_topup_intent(
    user: Any,
    amount_micro: int,
    currency: str = "usd",
    *,
    gateway: StripeGateway,
    idempotency: str | None = None,
    config: StripeTopupConfig | None = None,
    fx_provider: FxRateProvider | None = None,
    clock: Callable[[], float] = time.time,
) -> TopupIntent:
    """Create a PaymentIntent for a fee-balance top-up of ``amount_micro`` USD credit.

    ``idempotency`` should be a client-generated token per top-up attempt (the UI creates a UUID when the user
    opens the top-up form); retries with the same token return the same PaymentIntent, and a retry with a
    different amount fails with Conflict (Stripe idempotency_error). Header key: ``topup:{user_id}:{token}``.
    """
    cfg = config or StripeTopupConfig()
    user_id = _user_id_of(user)
    if not isinstance(amount_micro, int) or isinstance(amount_micro, bool):
        raise ValidationFailed("amount must be integer micro-USD")
    if amount_micro < cfg.min_topup_micro:
        raise ValidationFailed(f"minimum top-up is {fmt_usd(cfg.min_topup_micro)}", min_micro=cfg.min_topup_micro)
    if amount_micro > cfg.max_topup_micro:
        raise ValidationFailed(f"maximum top-up is {fmt_usd(cfg.max_topup_micro)}", max_micro=cfg.max_topup_micro)
    if amount_micro % USD_CENT_MICRO:
        raise ValidationFailed("amount must be whole cents")
    currency = (currency or "").lower()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValidationFailed("unsupported currency", currency=currency)
    token = idempotency or uuid.uuid4().hex
    if not _IDEMPOTENCY_RE.fullmatch(token):
        raise ValidationFailed("bad idempotency token")
    idem_key = f"topup:{user_id}:{token}"

    metadata: dict[str, str] = {
        "user_id": user_id,
        "purpose": PURPOSE_TOPUP,
        "idempotency": token,
        "credit_micro": str(amount_micro),
        "env": cfg.env,
    }
    quote: FxQuote | None = None
    applied_str: str | None = None
    if currency == "usd":
        amount_minor = amount_micro // USD_CENT_MICRO
    else:
        if not cfg.myr_enabled:
            raise ValidationFailed("MYR top-ups are not enabled")
        if fx_provider is None:
            raise ValidationFailed("no FX rate provider configured")
        try:
            quote = fx_provider.quote_usd_to(currency)
        except AppError:
            raise
        except Exception as e:
            raise ExternalServiceError("FX quote unavailable", error=type(e).__name__) from None
        rate = quote.rate if isinstance(quote.rate, Decimal) else None
        if rate is None or not rate.is_finite() or rate <= 0 or quote.currency.lower() != currency:
            raise ExternalServiceError("FX quote invalid")
        lo, hi = cfg.fx_sanity_bounds.get(currency, (Decimal(0), Decimal("Infinity")))
        if not (lo <= rate <= hi):
            raise ExternalServiceError("FX quote outside sanity bounds")
        age = clock() - quote.quoted_at
        if age > cfg.fx_quote_max_age_s or age < -5:
            raise ExternalServiceError("FX quote stale")
        amount_minor, applied = _myr_minor_for(amount_micro, rate, cfg.myr_fx_spread_bps)
        applied_str = format(applied.quantize(Decimal("0.00000001")), "f")
        metadata.update({
            "amount_minor": str(amount_minor),
            "fx_mid_rate": format(rate, "f")[:40],
            "fx_spread_bps": str(cfg.myr_fx_spread_bps),
            "fx_rate_applied": applied_str,
            "fx_source": str(quote.source)[:100],
            "fx_quote_id": str(quote.quote_id)[:200],
            "fx_quoted_at": str(int(quote.quoted_at)),
        })

    params = {
        "amount": amount_minor,
        "currency": currency,
        "automatic_payment_methods": {"enabled": True},
        "metadata": metadata,
        "description": f"aijalon.trade fee balance top-up {fmt_usd(amount_micro)}",
    }
    pi = gateway.create_payment_intent(params, idem_key)
    pi_id, secret = str(pi.get("id", "")), str(pi.get("client_secret", ""))
    if not pi_id.startswith("pi_") or not secret:
        raise ExternalServiceError("stripe returned an unexpected PaymentIntent")
    if int(pi.get("amount", -1)) != amount_minor or str(pi.get("currency", "")).lower() != currency:
        # Same idempotency key replayed against a different request would be caught by Stripe; this is belt+braces.
        raise Conflict("PaymentIntent does not match the request")
    log.info("stripe topup intent created", extra={"fields": {
        "user_id": user_id, "pi": pi_id, "currency": currency, "amount_minor": amount_minor, "credit_micro": amount_micro}})
    est = None
    if not cfg.stripe_fee_absorbed and cfg.fee_estimate_bps is not None:
        est = min(amount_micro, apply_bps_floor(amount_micro, cfg.fee_estimate_bps) + cfg.fee_estimate_fixed_micro)
    return TopupIntent(pi_id, secret, amount_micro, currency, amount_minor, idem_key, applied_str, quote,
                       fee_passthrough=not cfg.stripe_fee_absorbed, estimated_fee_micro=est)


# ==============================================================================================================
# Webhook signature verification
# ==============================================================================================================
class WebhookVerificationError(AppError):
    http_status, code = 400, "invalid_webhook_signature"


def compute_signature(payload: bytes, timestamp: int, secret: str) -> str:
    """Stripe scheme: v1 = hex(HMAC-SHA256(key=endpoint secret, msg=f"{t}." + raw payload))."""
    return hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + payload, hashlib.sha256).hexdigest()


def _parse_sig_header(header: str) -> tuple[int, list[str]]:
    if not header or not isinstance(header, str) or len(header) > 4096:
        raise WebhookVerificationError("missing or oversized Stripe-Signature header")
    ts: int | None = None
    v1: list[str] = []
    for part in header.split(","):
        k, sep, v = part.strip().partition("=")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if k == "t":
            if ts is not None or not v.isdigit() or len(v) > 12:
                raise WebhookVerificationError("bad timestamp in signature header")
            ts = int(v)
        elif k == "v1":
            if _HEX64_RE.fullmatch(v.lower()):
                v1.append(v.lower())
    if ts is None:
        raise WebhookVerificationError("no timestamp in signature header")
    if not v1:
        raise WebhookVerificationError("no v1 signature in header")
    if len(v1) > 16:
        raise WebhookVerificationError("too many signatures")
    return ts, v1


def verify_webhook(
    payload_bytes: bytes,
    sig_header: str,
    secret: str | Sequence[str],
    tolerance: int = 300,
    *,
    now: float | None = None,
    use_stripe_lib: bool | None = None,
) -> dict:
    """Verify a Stripe webhook on the RAW request body and return the parsed event.

    * Accepts several ``v1`` signatures in the header (Stripe sends one per active endpoint secret while a
      secret is being rolled) and several local secrets (pass old + new during rotation).
    * Constant-time comparison (hmac.compare_digest) against every candidate.
    * Rejects timestamps older than ``tolerance`` seconds (replay window) and, stricter than the Stripe lib,
      timestamps more than ``tolerance`` in the future. Replays INSIDE the window are harmless because every
      instruction is idempotent on its ledger key.
    * When the ``stripe`` library is installed (prod) ``stripe.Webhook.construct_event`` must ALSO accept it.
    """
    if not isinstance(payload_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("pass the raw request body as bytes (never a re-serialized JSON string)")
    payload = bytes(payload_bytes)
    secrets = [secret] if isinstance(secret, str) else [s for s in secret]
    secrets = [s for s in secrets if isinstance(s, str) and s]
    if not secrets:
        raise WebhookVerificationError("webhook secret not configured")
    ts, candidates = _parse_sig_header(sig_header)
    now_s = time.time() if now is None else now
    if tolerance and tolerance > 0:
        if now_s - ts > tolerance:
            raise WebhookVerificationError("timestamp outside tolerance (too old)")
        if ts - now_s > tolerance:
            raise WebhookVerificationError("timestamp outside tolerance (in the future)")

    matched: str | None = None
    for s in secrets:
        expected = compute_signature(payload, ts, s)
        ok = False
        for cand in candidates:  # check all; no early exit on the inner loop
            ok = hmac.compare_digest(expected, cand) or ok
        if ok:
            matched = s
            break
    if matched is None:
        raise WebhookVerificationError("signature mismatch")

    if use_stripe_lib is None:
        use_stripe_lib = _stripe_lib() is not None
    if use_stripe_lib:
        lib = _stripe_lib()
        if lib is None:
            raise WebhookVerificationError("stripe library requested but not installed")
        try:
            lib.Webhook.construct_event(payload.decode("utf-8"), sig_header, matched, tolerance=tolerance)
        except Exception as e:
            raise WebhookVerificationError("stripe library rejected signature", error=type(e).__name__) from None

    try:
        event = json.loads(payload.decode("utf-8"))
    except Exception:
        raise WebhookVerificationError("payload is not JSON") from None
    if not isinstance(event, dict) or event.get("object") != "event" or not event.get("id") or not event.get("type"):
        raise WebhookVerificationError("payload is not a Stripe event")
    return event


# ==============================================================================================================
# Event handling
# ==============================================================================================================
@dataclass(frozen=True)
class DepositRecord:
    """What we know about an original top-up (from our ``deposits`` table or the PaymentIntent metadata)."""

    user_id: str
    payment_intent_id: str
    currency: str
    amount_minor: int      # amount charged (cents / sen)
    credit_micro: int      # USD credited for the full amount


DepositLookup = Callable[[str], "DepositRecord | None"]  # by payment_intent id


@dataclass
class WebhookOutcome:
    event_id: str
    event_type: str
    credits: list[CreditInstruction] = field(default_factory=list)
    debits: list[DebitInstruction] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    ignored: str | None = None
    manual_review: str | None = None

    @property
    def instructions(self) -> list[CreditInstruction | DebitInstruction]:
        return [*self.credits, *self.debits]


def _obj(event: Mapping[str, Any]) -> dict:
    data = event.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("object"), Mapping):
        raise ValidationFailed("event has no data.object")
    return dict(data["object"])


def _int(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValidationFailed(f"bad integer field {name}")
    return v


def _record_from_meta(meta: Mapping[str, Any], pi_id: str, currency: str, amount_minor: int) -> DepositRecord | None:
    """Rebuild the deposit record from our own metadata. Returns None if it is not our top-up or incomplete."""
    if not isinstance(meta, Mapping) or meta.get("purpose") != PURPOSE_TOPUP or not meta.get("user_id"):
        return None
    currency = currency.lower()
    if currency == "usd":
        return DepositRecord(str(meta["user_id"]), pi_id, "usd", amount_minor, amount_minor * USD_CENT_MICRO)
    if currency == "myr":
        try:
            credit = int(str(meta["credit_micro"]))
            charged = int(str(meta["amount_minor"]))
        except (KeyError, ValueError):
            return None
        if credit <= 0 or charged <= 0 or charged != amount_minor:
            return None
        return DepositRecord(str(meta["user_id"]), pi_id, "myr", charged, credit)
    return None


def _credit_for(record: DepositRecord, minor: int) -> int:
    """USD micro for ``minor`` units of the original charge. Exact for USD; pro-rata (floor) of the locked credit
    for MYR, so cumulative reversals telescope to exactly the credited amount."""
    if record.currency == "usd":
        return minor * USD_CENT_MICRO
    minor = min(minor, record.amount_minor)
    return record.credit_micro * minor // record.amount_minor


def _ops_alert(kind: str, severity: Severity, key: str, **data: Any) -> Alert:
    return Alert(kind=kind, severity=severity, user_id=None, data=data, key=key)


def _manual(out: WebhookOutcome, reason: str) -> WebhookOutcome:
    out.manual_review = reason
    out.alerts.append(_ops_alert("payment_manual_review", Severity.CRITICAL, f"stripe_manual:{out.event_id}",
                                 event=out.event_id, event_type=out.event_type, reason=reason))
    log.error("stripe event needs manual review", extra={"fields": {"event": out.event_id, "type": out.event_type, "reason": reason}})
    return out


def _resolve_record(pi_id: str | None, meta: Mapping[str, Any] | None, currency: str, amount_minor: int | None,
                    lookup: DepositLookup | None, gateway: StripeGateway | None) -> DepositRecord | None:
    if lookup is not None and pi_id:
        rec = lookup(pi_id)
        if rec is not None:
            return rec
    if meta and amount_minor is not None:
        rec = _record_from_meta(meta, pi_id or "", currency, amount_minor)
        if rec is not None:
            return rec
    if gateway is not None and pi_id:
        pi = gateway.retrieve_payment_intent(pi_id)
        return _record_from_meta(pi.get("metadata") or {}, pi_id, str(pi.get("currency", "")), int(pi.get("amount", 0)))
    return None


def handle_event(
    event: Mapping[str, Any],
    *,
    config: StripeTopupConfig | None = None,
    lookup: DepositLookup | None = None,
    gateway: StripeGateway | None = None,
    fee_lookup: Callable[[dict], int] | None = None,
) -> WebhookOutcome:
    """Map a VERIFIED Stripe event to ledger instructions + alerts. Pure apart from optional ports:

    * ``gateway`` — ``payment_intent.succeeded`` re-fetches the PaymentIntent from Stripe (expanded with
      ``latest_charge.balance_transaction``) and uses that: the actual fee for the pass-through credit, and
      defence in depth against a leaked webhook secret. REQUIRED when fees are passed through (the default):
      without it (or ``fee_lookup``) the event raises and Stripe retries. Also resolves disputes.
    * ``lookup`` — our ``deposits`` table by PaymentIntent id (preferred source for refunds/disputes).
    * ``fee_lookup`` — override: returns the Stripe fee in micro-USD for a (re-fetched) PaymentIntent.

    Raises ``StripeFeeNotReady`` / ``ExternalServiceError`` for conditions that should be retried: the API must
    answer non-2xx so Stripe redelivers (nothing is credited in the meantime).

    Credit = gross − actual fee (SPEC §1). Reversals (refunds, disputes) debit the GROSS value of the refunded or
    disputed amount: that is the money that went back to the user, and Stripe does not return its fee. A
    refund issued per Terms 10.9 (net of the processor fee) therefore reverses exactly the credit; a chargeback
    of the full gross leaves the balance negative by the fee (→ reduce-only until topped up).

    Idempotency keys: credit ``stripe:{pi}``; refund ``stripe:refund:{charge}:{cumulative_refunded_minor}``;
    dispute ``stripe:dispute:{dispute}``; dispute reinstated ``stripe:dispute_reinstated:{dispute}``.
    Delivering the same event twice yields identical instructions; the ledger's unique idempotency_key makes the
    second post a no-op. Unknown event types are ignored (return 2xx so Stripe stops retrying).
    """
    cfg = config or StripeTopupConfig()
    event_id, etype = str(event.get("id", "")), str(event.get("type", ""))
    out = WebhookOutcome(event_id, etype)
    if cfg.livemode_required is not None and bool(event.get("livemode")) != cfg.livemode_required:
        out.ignored = "livemode mismatch"
        out.alerts.append(_ops_alert("payment_manual_review", Severity.CRITICAL, "stripe_livemode_mismatch",
                                     event=event_id, event_type=etype,
                                     reason="event livemode does not match this environment (check webhook secret/endpoint)"))
        return out
    if etype not in HANDLED_EVENTS:
        out.ignored = "unhandled event type"
        return out
    obj = _obj(event)
    if etype == "payment_intent.succeeded":
        return _on_pi_succeeded(out, obj, cfg, gateway, fee_lookup)
    if etype == "payment_intent.payment_failed":
        return _on_pi_failed(out, obj, cfg)
    if etype == "charge.refunded":
        prev = (event.get("data") or {}).get("previous_attributes") or {}
        return _on_charge_refunded(out, obj, prev, cfg, lookup, gateway)
    if etype == "charge.dispute.created":
        return _on_dispute_created(out, obj, lookup, gateway)
    return _on_dispute_closed(out, obj, lookup, gateway)


def _env_ok(meta: Mapping[str, Any], cfg: StripeTopupConfig) -> bool:
    env = meta.get("env")
    return env is None or env == cfg.env


def _on_pi_succeeded(out: WebhookOutcome, pi: dict, cfg: StripeTopupConfig, gateway: StripeGateway | None,
                     fee_lookup: Callable[[dict], int] | None) -> WebhookOutcome:
    pi_id = str(pi.get("id", ""))
    if pi.get("object") != "payment_intent" or not pi_id.startswith("pi_"):
        return _manual(out, "malformed payment_intent object")
    meta = pi.get("metadata") or {}
    if meta.get("purpose") != PURPOSE_TOPUP:
        out.ignored = "not a fee-balance top-up"
        return out
    if not _env_ok(meta, cfg):
        out.ignored = "top-up belongs to another environment"
        return out
    if not cfg.stripe_fee_absorbed and gateway is None and fee_lookup is None:
        raise ExternalServiceError("stripe fee pass-through needs a gateway to read the balance transaction")
    if gateway is not None:
        fetched = gateway.retrieve_payment_intent(pi_id, expand=FEE_EXPAND if not cfg.stripe_fee_absorbed else ())
        if fetched.get("id") != pi_id:
            return _manual(out, "re-fetched PaymentIntent id mismatch")
        pi, meta = fetched, fetched.get("metadata") or {}
        if meta.get("purpose") != PURPOSE_TOPUP:
            return _manual(out, "re-fetched PaymentIntent lost top-up metadata")
    if pi.get("status") != "succeeded":
        return _manual(out, f"payment_intent.succeeded but status={str(pi.get('status'))[:30]}")
    user_id = str(meta.get("user_id") or "")
    if not user_id:
        return _manual(out, "top-up without user_id")
    currency = str(pi.get("currency", "")).lower()
    if currency not in SUPPORTED_CURRENCIES:
        return _manual(out, f"unsupported currency {currency[:10]}")
    try:
        amount = _int(pi.get("amount"), "amount")
        received = _int(pi.get("amount_received"), "amount_received")
    except ValidationFailed as e:
        return _manual(out, e.message)
    record = _record_from_meta(meta, pi_id, currency, amount)
    if record is None:
        return _manual(out, "top-up metadata incomplete (FX lock missing?)")
    if received <= 0:
        return _manual(out, "amount_received is zero")
    credit = _credit_for(record, received)
    if received != amount:
        out.alerts.append(_ops_alert("payment_manual_review", Severity.WARN, f"stripe_partial:{pi_id}", event=out.event_id,
                                     event_type=out.event_type, reason=f"received {received} of {amount} {currency} minor units; credited pro-rata"))
    gross = credit
    fee_micro = 0
    if not cfg.stripe_fee_absorbed:
        try:
            fee_micro = int(fee_lookup(pi)) if fee_lookup is not None else stripe_fee_micro(pi, gross)
        except StripeFeeNotReady:
            raise
        except (ValidationFailed, TypeError, ValueError) as e:
            return _manual(out, f"Stripe fee unreadable: {getattr(e, 'message', type(e).__name__)}")
        if fee_micro < 0 or fee_micro >= gross:
            return _manual(out, "Stripe fee out of range")
        credit = gross - fee_micro
    out.credits.append(CreditInstruction(
        user_id=user_id,
        amount_micro=credit,
        external_ref=pi_id,
        idempotency_key=f"stripe:{pi_id}",
        method="stripe",
        debit_account=STRIPE_CLEARING,
        credit_account=user_fee_balance_account(user_id),
        kind="deposit",
        memo=f"Stripe top-up {pi_id} ({received} {currency} minor; gross {fmt_usd(gross)}, processor fee {fmt_usd(fee_micro)})",
        withdrawable=False,
        meta={"currency": currency, "amount_minor": received, "gross_micro": gross, "fee_micro": fee_micro,
              "fx_rate_applied": meta.get("fx_rate_applied"), "event_id": out.event_id},
    ))
    out.alerts.append(Alert(kind="topup_credited", severity=Severity.INFO, user_id=user_id,
                            data={"amount_micro": credit, "method": "card/wallet"}, key=f"topup_credited:{pi_id}"))
    return out


def _on_pi_failed(out: WebhookOutcome, pi: dict, cfg: StripeTopupConfig) -> WebhookOutcome:
    meta = pi.get("metadata") or {}
    if meta.get("purpose") != PURPOSE_TOPUP or not meta.get("user_id") or not _env_ok(meta, cfg):
        out.ignored = "not a fee-balance top-up"
        return out
    out.ignored = "payment failed; nothing to credit"
    out.alerts.append(Alert(kind="topup_failed", severity=Severity.INFO, user_id=str(meta["user_id"]),
                            data={"method": "card/wallet"}, key=f"topup_failed:{pi.get('id')}"))
    return out


def _on_charge_refunded(out: WebhookOutcome, ch: dict, prev: Mapping[str, Any], cfg: StripeTopupConfig,
                        lookup: DepositLookup | None, gateway: StripeGateway | None) -> WebhookOutcome:
    ch_id, pi_id = str(ch.get("id", "")), ch.get("payment_intent")
    pi_id = str(pi_id) if isinstance(pi_id, str) else None
    meta = ch.get("metadata") or {}
    if meta and meta.get("purpose") not in (None, PURPOSE_TOPUP):
        out.ignored = "not a fee-balance top-up"
        return out
    if meta and not _env_ok(meta, cfg):
        out.ignored = "top-up belongs to another environment"
        return out
    try:
        amount = _int(ch.get("amount"), "amount")
        cum = _int(ch.get("amount_refunded"), "amount_refunded")
    except ValidationFailed as e:
        return _manual(out, e.message)
    if "amount_refunded" not in prev:
        return _manual(out, f"charge.refunded on {ch_id} without previous_attributes.amount_refunded; reconcile by hand")
    try:
        prev_cum = _int(prev.get("amount_refunded"), "previous amount_refunded")
    except ValidationFailed as e:
        return _manual(out, e.message)
    if cum <= prev_cum:
        out.ignored = "no new refunded amount"
        return out
    record = _resolve_record(pi_id, meta, str(ch.get("currency", "")), amount, lookup, gateway)
    if record is None:
        if not meta:
            out.ignored = "charge not identifiable as a top-up"
            out.alerts.append(_ops_alert("payment_manual_review", Severity.WARN, f"stripe_unknown_refund:{ch_id}",
                                         event=out.event_id, event_type=out.event_type, reason=f"refund on unidentified charge {ch_id}"))
            return out
        return _manual(out, f"cannot resolve top-up for refunded charge {ch_id}")
    debit = _credit_for(record, cum) - _credit_for(record, prev_cum)
    if debit <= 0:
        out.ignored = "refund rounds to zero"
        return out
    out.debits.append(DebitInstruction(
        user_id=record.user_id,
        amount_micro=debit,
        external_ref=ch_id,
        idempotency_key=f"stripe:refund:{ch_id}:{cum}",
        method="stripe",
        debit_account=user_fee_balance_account(record.user_id),
        credit_account=STRIPE_CLEARING,
        kind="stripe_refund",
        memo=f"Stripe refund on {ch_id} (cumulative {cum} {record.currency} minor)",
        meta={"payment_intent": record.payment_intent_id, "cumulative_minor": cum, "event_id": out.event_id},
    ))
    out.alerts.append(_ops_alert("stripe_refund_ops", Severity.CRITICAL, f"stripe_refund:{ch_id}:{cum}",
                                 charge=ch_id, user=record.user_id, amount_micro=debit))
    out.alerts.append(Alert(kind="stripe_refund", severity=Severity.WARN, user_id=record.user_id,
                            data={"amount_micro": debit}, key=f"stripe_refund_user:{ch_id}:{cum}"))
    return out


def _dispute_record(dp: dict, lookup: DepositLookup | None, gateway: StripeGateway | None) -> DepositRecord | None:
    pi_id = dp.get("payment_intent")
    pi_id = str(pi_id) if isinstance(pi_id, str) else None
    return _resolve_record(pi_id, None, str(dp.get("currency", "")), None, lookup, gateway)


def _on_dispute_created(out: WebhookOutcome, dp: dict, lookup: DepositLookup | None,
                        gateway: StripeGateway | None) -> WebhookOutcome:
    dp_id, ch_id = str(dp.get("id", "")), str(dp.get("charge", ""))
    try:
        amount = _int(dp.get("amount"), "amount")
    except ValidationFailed as e:
        return _manual(out, e.message)
    record = _dispute_record(dp, lookup, gateway)
    if record is None:
        return _manual(out, f"cannot resolve top-up for dispute {dp_id} on {ch_id}; freeze the user's balance by hand")
    if str(dp.get("currency", "")).lower() != record.currency:
        return _manual(out, f"dispute {dp_id} currency differs from the top-up")
    debit = _credit_for(record, amount)
    if debit <= 0:
        out.ignored = "dispute amount rounds to zero"
        return out
    out.debits.append(DebitInstruction(
        user_id=record.user_id,
        amount_micro=debit,
        external_ref=dp_id,
        idempotency_key=f"stripe:dispute:{dp_id}",
        method="stripe",
        debit_account=user_fee_balance_account(record.user_id),
        credit_account=STRIPE_CLEARING,
        kind="stripe_dispute",
        memo=f"Stripe dispute {dp_id} on {ch_id} ({str(dp.get('reason', ''))[:40]})",
        meta={"payment_intent": record.payment_intent_id, "status": dp.get("status"), "event_id": out.event_id},
    ))
    out.alerts.append(_ops_alert("stripe_dispute_ops", Severity.CRITICAL, f"stripe_dispute:{dp_id}", dispute=dp_id,
                                 reason=str(dp.get("reason", "unknown")), charge=ch_id, user=record.user_id, amount_micro=debit))
    out.alerts.append(Alert(kind="stripe_dispute", severity=Severity.WARN, user_id=record.user_id,
                            data={"amount_micro": debit}, key=f"stripe_dispute_user:{dp_id}"))
    return out


def _on_dispute_closed(out: WebhookOutcome, dp: dict, lookup: DepositLookup | None,
                       gateway: StripeGateway | None) -> WebhookOutcome:
    dp_id, status = str(dp.get("id", "")), str(dp.get("status", ""))
    try:
        amount = _int(dp.get("amount"), "amount")
    except ValidationFailed as e:
        return _manual(out, e.message)
    record = _dispute_record(dp, lookup, gateway)
    user = record.user_id if record else "unknown"
    amount_micro = _credit_for(record, amount) if record else 0
    if status not in ("won", "warning_closed"):
        # lost / other: the debit from dispute.created stands.
        out.ignored = f"dispute closed with status {status[:30]}; debit stands"
        out.alerts.append(_ops_alert("stripe_dispute_closed_ops", Severity.INFO, f"stripe_dispute_closed:{dp_id}",
                                     dispute=dp_id, status=status, user=user, amount_micro=amount_micro))
        return out
    if record is None:
        return _manual(out, f"dispute {dp_id} {status}: cannot resolve top-up to re-credit")
    if amount_micro <= 0:
        out.ignored = "dispute amount rounds to zero"
        return out
    out.credits.append(CreditInstruction(
        user_id=record.user_id,
        amount_micro=amount_micro,
        external_ref=dp_id,
        idempotency_key=f"stripe:dispute_reinstated:{dp_id}",
        method="stripe",
        debit_account=STRIPE_CLEARING,
        credit_account=user_fee_balance_account(record.user_id),
        kind="stripe_dispute_reinstated",
        memo=f"Stripe dispute {dp_id} closed {status}; balance reinstated",
        withdrawable=False,
        meta={"payment_intent": record.payment_intent_id, "status": status, "event_id": out.event_id},
    ))
    out.alerts.append(_ops_alert("stripe_dispute_closed_ops", Severity.INFO, f"stripe_dispute_closed:{dp_id}",
                                 dispute=dp_id, status=status, user=user, amount_micro=amount_micro))
    return out
