"""Request/response models (pydantic v2). Every request model forbids unknown fields.

Money in: either `<name>_micro` (strict integer micro-USD) or `<name>` (decimal USD string, ≤ 6 dp) — exactly one.
Money out: always integer micro-USD (`*_micro`). Hyperliquid quantities (sizes, prices) are returned as strings.
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.api import validation as v

T = TypeVar("T")

# ---------------------------------------------------------------------------------------------------- primitives


def _addr(value: str) -> str:
    try:
        return v.normalize_address(value)
    except v.InputError as e:
        raise ValueError(str(e)) from None


def _clean_line(value: str) -> str:
    """Single-line user text: strip, NFC-normalize, reject control characters."""
    value = unicodedata.normalize("NFC", value.strip())
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        raise ValueError("control characters are not allowed")
    return value


def _clean_text(value: str) -> str:
    """Multi-line user text: allow \\n and \\t, reject other control/format characters."""
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").strip())
    if any(unicodedata.category(ch) in ("Cc", "Cf") and ch not in "\n\t" for ch in value):
        raise ValueError("control characters are not allowed")
    return value


Address = Annotated[str, StringConstraints(min_length=42, max_length=42), AfterValidator(_addr)]
TxHash = Annotated[str, StringConstraints(pattern=v.TX_HASH_RE.pattern)]
Signature = Annotated[str, StringConstraints(pattern=v.SIGNATURE_RE.pattern)]
ChainIdHex = Annotated[str, StringConstraints(pattern=v.CHAIN_ID_RE.pattern)]
UsdString = Annotated[str, StringConstraints(pattern=v.USD_STRING_RE.pattern)]
Micro = Annotated[StrictInt, Field(ge=1, le=v.MAX_AMOUNT_MICRO)]
MicroOrZero = Annotated[StrictInt, Field(ge=0, le=v.MAX_AMOUNT_MICRO)]
Slug = Annotated[str, StringConstraints(pattern=v.SLUG_RE.pattern)]
Coin = Annotated[str, StringConstraints(pattern=v.COIN_RE.pattern)]
Country = Annotated[str, StringConstraints(pattern=v.COUNTRY_RE.pattern)]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=64), AfterValidator(_clean_line)]
Title = Annotated[str, StringConstraints(min_length=3, max_length=140), AfterValidator(_clean_line)]
Reason = Annotated[str, StringConstraints(min_length=5, max_length=500), AfterValidator(_clean_text)]
DocVersion = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.\-]{1,32}$")]

LegalDoc = Literal["terms", "risk", "privacy", "jurisdiction", "waiver", "creator_agreement"]
PlanKey = Literal["free", "pro", "max"]


def _one_amount(micro: Optional[int], text: Optional[str], name: str, *, allow_zero: bool = False,
                required: bool = True) -> Optional[int]:
    """Exactly one of `<name>_micro` / `<name>` → micro int."""
    if micro is not None and text is not None:
        raise ValueError(f"send either {name}_micro or {name}, not both")
    if micro is None and text is None:
        if required:
            raise ValueError(f"{name}_micro or {name} is required")
        return None
    try:
        if micro is not None:
            return v.check_micro(micro, allow_zero=allow_zero)
        assert text is not None
        if allow_zero and text in ("0", "0.0", "0.00"):
            return 0
        return v.usd_string_to_micro(text)
    except v.InputError as e:
        raise ValueError(str(e)) from None


class In(BaseModel):
    """Base for request bodies: unknown fields are rejected; strings are not silently coerced from numbers."""
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------------------------------- errors / misc
class ErrorBody(BaseModel):
    code: str
    message: str
    details: Optional[dict[str, Any]] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: Optional[str] = None


class Ok(Out):
    ok: bool = True


# ---------------------------------------------------------------------------------------------------- public
class StrategyStats(Out):
    subscribers: Optional[int] = None           # hidden (None) below the k-anonymity floor
    roi_bps: Optional[int] = None
    pnl_micro: Optional[int] = None
    since: Optional[datetime] = None            # live record starts at current version's live_since
    hidden_reason: Optional[str] = None


class StrategySummary(Out):
    id: UUID
    slug: str
    name: str
    description: Optional[str] = None
    in_house: bool
    markets: list[str]
    timeframe: str
    status: str
    price_monthly_micro: Optional[int]
    profit_share_bps: int
    platform_profit_share_bps: int
    platform_profit_share_mode: str
    holds: Optional[bool] = None                # True = "HOLDS — no active signals"
    signal_state: Literal["trades", "holds", "unknown"] = "unknown"   # same information as `holds`, for badges
    current_version: Optional[int] = None
    live_since: Optional[datetime] = None
    live_days: Optional[int] = None             # whole days since the current version's live_since
    not_live_proven: bool = True                # < 90 days of live signals → "not proven live" warning
    max_leverage: Optional[int] = None          # current version's MAX_LEVERAGE (effective cap = min with platform/launch)
    history_days: Optional[int] = None          # backtestable history of the current version (None = unknown/long)
    short_history_days: Optional[int] = None    # = history_days when < risk.short_history_warning_days (SPEC §12)
    free_showcase: bool = False                 # in-house, $0 and 0% profit share (SPEC §12 SILVER)
    showcase_text: Optional[str] = None         # plain statement shown on the card and page when free_showcase
    stats: StrategyStats


class StrategyVersionPublic(Out):
    version: int
    published_at: Optional[datetime]
    live_since: Optional[datetime]
    is_current: bool


class StrategyDetail(StrategySummary):
    versions: list[StrategyVersionPublic]
    backtest: Optional[dict[str, Any]] = None   # sandbox report (app.sandbox.backtest) minus trades/latest_signal/data_notes
    backtest_warning: Optional[str] = None
    risk_ack_text: str                          # strategy-specific acknowledgement shown by the subscribe gate
    rating_avg_x100: Optional[int] = None
    rating_count: int = 0


class LeaderboardEntry(Out):
    rank: int
    slug: str
    name: str
    roi_bps: Optional[int]
    pnl_micro: Optional[int]
    subscribers: Optional[int]


class LeaderboardOut(Out):
    by: str
    period: str
    entries: list[LeaderboardEntry]


class PostSummary(Out):
    id: UUID
    title: str
    price_micro: int
    strategy_slug: Optional[str] = None
    creator_display_name: Optional[str] = None
    published_at: Optional[datetime] = None
    preview: Optional[str] = None               # only for free posts


class PlanOut(Out):
    key: str
    price_monthly_micro: int
    max_active_strategies: Optional[int]
    features: list[str]


class EconomicsOut(Out):
    builder_fee_tenths_bp: int
    builder_split_creator_bps: int
    builder_split_platform_bps: int
    builder_split_referral_pool_bps: int
    profit_share_creator_cap_bps: int
    platform_profit_share_bps: int
    platform_profit_share_mode: str
    subscription_platform_bps: int
    post_platform_fee_micro: int
    post_min_price_micro: int
    min_topup_micro: int
    past_due_grace_hours: int
    stripe_fee_absorbed: bool


class ReferralTierOut(Out):
    name: str
    min_active_users: int
    min_notional_30d_micro: int
    share_of_pool_bps: int


class PublicConfigOut(Out):
    builder_address: str
    treasury_address: str
    agent_name: str
    hl_chain: Literal["Mainnet", "Testnet"]
    stripe_publishable_key: Optional[str] = None
    # Stripe fees are passed to the user (SPEC §1): estimate shown before paying; names = config.Settings fields.
    # Both null when unknown (not configured) or when economics.stripe_fee_absorbed.
    stripe_fee_estimate_bps: Optional[int] = None
    stripe_fee_estimate_fixed_micro: Optional[int] = None
    restricted_jurisdictions: list[str]
    legal_versions: dict[str, str]              # consent doc key (terms, risk, privacy, …) → current version
    economics: EconomicsOut
    plans: list[PlanOut]
    referral_tiers: list[ReferralTierOut]
    features: dict[str, bool]
    platform_max_leverage: int
    max_user_leverage_x100: Optional[int] = None   # launch-phase cap (None = platform cap only)
    min_allocation_micro: int
    min_listing_history_days: int
    short_history_warning_days: int
    launch_phase: str


class ShowcaseWalletOut(Out):
    address: str
    period_month: date
    revealed_at: datetime


class ReviewOut(Out):
    id: UUID
    rating: int
    body: Optional[str]
    author: str
    created_at: datetime


# ---------------------------------------------------------------------------------------------------- me / consents
class WalletOut(Out):
    address: str
    verified_at: Optional[datetime]


class MeOut(Out):
    id: UUID
    email: Optional[str]
    display_name: Optional[str]
    role: str
    plan: str
    status: str
    referral_code: str
    country_attested: Optional[str]
    mfa_enrolled: bool
    created_at: datetime
    consents_complete: bool
    wallets: list[WalletOut]
    kyc_status: Optional[str] = None            # creator KYC: pending | provider_approved (awaiting our admin) |
                                                # approved | rejected (None = not started)


class MePatchIn(In):
    display_name: Optional[DisplayName] = None
    referral_code_used: Optional[Annotated[str, StringConstraints(min_length=4, max_length=32)]] = None

    @model_validator(mode="after")
    def _something(self) -> "MePatchIn":
        if self.display_name is None and self.referral_code_used is None:
            raise ValueError("nothing to change")
        return self


class PlanChangeIn(In):
    plan: PlanKey


class PlanChangeOut(Out):
    plan: str
    charged_micro: int
    period_end: Optional[datetime]
    fee_balance_micro: int


Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ConsentDoc = Literal["terms", "risk", "privacy", "jurisdiction", "waiver", "creator_agreement", "subscription_ack"]
SITE_DOC_NAMES = ("terms", "risk", "privacy", "jurisdiction", "waiver")


class ConsentItem(In):
    doc: ConsentDoc
    doc_version: DocVersion
    context: Literal["site_entry", "subscribe", "creator"]
    strategy_id: Optional[UUID] = None
    accepted_at: Optional[Annotated[str, StringConstraints(max_length=40)]] = None   # client clock; audit only
    doc_text_sha256: Sha256Hex                      # sha256 of the exact legal/<file>.md bytes the user was shown
    country: Optional[Country] = None               # optional residence attestation (jurisdiction doc only)

    @model_validator(mode="after")
    def _shape(self) -> "ConsentItem":
        if self.doc == "subscription_ack":
            if self.context != "subscribe" or self.strategy_id is None:
                raise ValueError("subscription_ack needs context 'subscribe' and a strategy_id")
        elif self.doc == "creator_agreement":
            if self.context != "creator" or self.strategy_id is not None:
                raise ValueError("creator_agreement needs context 'creator' and no strategy_id")
        else:
            if self.context == "creator":
                raise ValueError("site documents use context 'site_entry' or 'subscribe'")
            if self.context == "site_entry" and self.strategy_id is not None:
                raise ValueError("strategy_id is only allowed with context 'subscribe'")
        if self.country and self.doc != "jurisdiction":
            raise ValueError("country is only accepted with the jurisdiction attestation")
        return self


class ConsentBatchIn(In):
    consents: list[ConsentItem] = Field(min_length=1, max_length=10)

    @field_validator("consents")
    @classmethod
    def _unique_docs(cls, items: list[ConsentItem]) -> list[ConsentItem]:
        keys = [(i.doc, i.strategy_id) for i in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate doc in batch")
        return items


class ConsentStatusOut(Out):
    required: dict[str, str]
    accepted: dict[str, str]
    missing: list[str]
    complete: bool


# ---------------------------------------------------------------------------------------------------- wallets / agents
class WalletNonceOut(Out):
    nonce: str
    expires_at: datetime


class WalletVerifyIn(In):
    address: Address
    message: Annotated[str, StringConstraints(min_length=40, max_length=2000)]   # EIP-4361 (SIWE) text
    signature: Signature


class AgentCreateIn(In):
    master_address: Address
    signature_chain_id: ChainIdHex
    rotate: StrictBool = False


class AgentOut(Out):
    id: UUID
    master_address: str
    agent_address: str
    agent_name: str
    status: str
    approved_at: Optional[datetime] = None
    created_at: datetime


class AgentCreateOut(Out):
    agent: AgentOut
    approve_agent: dict[str, Any]               # {action, typed_data} for the browser wallet to sign
    approve_builder_fee: Optional[dict[str, Any]] = None
    exchange_url: str
    required_builder_fee_tenths_bp: int


class BuilderApprovalConfirmIn(In):
    master_address: Address


class BuilderApprovalOut(Out):
    master_address: str
    max_fee_rate_tenths_bp: int
    required_tenths_bp: int
    sufficient: bool
    verified_on_chain_at: Optional[datetime]


# ---------------------------------------------------------------------------------------------------- subscriptions
MIN_ALLOCATION_MICRO = 100 * 1_000_000          # $100: below this, min order size ($10) makes sizing meaningless


class SubscriptionAck(In):
    """Strategy-specific risk acknowledgement + the exact fees the user was shown (must match current terms)."""
    version: DocVersion
    doc_text_sha256: Sha256Hex
    accepted: Literal[True]
    quoted_price_monthly_micro: MicroOrZero
    quoted_profit_share_bps: Annotated[StrictInt, Field(ge=0, le=10_000)]
    quoted_platform_profit_share_bps: Annotated[StrictInt, Field(ge=0, le=10_000)]
    quoted_builder_fee_tenths_bp: Annotated[StrictInt, Field(ge=0, le=1000)]


class SubscriptionCreateIn(In):
    strategy_id: UUID
    trading_address: Address
    allocation_micro: Optional[Micro] = None
    allocation: Optional[UsdString] = None
    max_leverage_x100: Annotated[StrictInt, Field(ge=100, le=2000)]
    # Either an inline ack, or a subscription_ack consent for this strategy recorded via POST /consents in the
    # last 30 minutes (the web subscribe gate does the latter). expected_* guard against terms changing mid-flow.
    ack: Optional[SubscriptionAck] = None
    expected_price_monthly_micro: Optional[MicroOrZero] = None
    expected_profit_share_bps: Optional[Annotated[StrictInt, Field(ge=0, le=10_000)]] = None

    @model_validator(mode="after")
    def _amount(self) -> "SubscriptionCreateIn":
        micro = _one_amount(self.allocation_micro, self.allocation, "allocation")
        assert micro is not None
        if micro < MIN_ALLOCATION_MICRO:
            raise ValueError("allocation below minimum ($100)")
        self.allocation_micro = micro
        self.allocation = None
        return self


class SubscriptionPatchIn(In):
    allocation_micro: Optional[Micro] = None
    allocation: Optional[UsdString] = None
    max_leverage_x100: Optional[Annotated[StrictInt, Field(ge=100, le=2000)]] = None
    paused: Optional[StrictBool] = None

    @model_validator(mode="after")
    def _amount(self) -> "SubscriptionPatchIn":
        micro = _one_amount(self.allocation_micro, self.allocation, "allocation", required=False)
        if micro is not None and micro < MIN_ALLOCATION_MICRO:
            raise ValueError("allocation below minimum ($100)")
        self.allocation_micro = micro
        self.allocation = None
        if micro is None and self.max_leverage_x100 is None and self.paused is None:
            raise ValueError("nothing to change")
        return self


class SubscriptionCancelIn(In):
    positions: Literal["close", "leave"]        # required, no default (SPEC §12)


class SubscriptionOut(Out):
    id: UUID
    strategy_id: UUID
    strategy_slug: Optional[str] = None
    strategy_name: Optional[str] = None
    strategy_markets: list[str] = Field(default_factory=list)
    trading_address: str
    allocation_micro: int
    max_leverage_x100: int
    status: str
    cancel_positions: Optional[str] = None      # close | leave once cancelled/closing (SPEC §12)
    cancelled_at: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    cum_pnl_micro: int = 0
    hwm_micro: int = 0
    created_at: datetime


class SubscriptionCreateOut(Out):
    subscription: SubscriptionOut
    charged_micro: int
    fee_balance_micro: int


# ---------------------------------------------------------------------------------------------------- balance / deposits
class BalanceOut(Out):
    fee_balance_micro: int
    withdrawable_micro: int                     # USDC-funded part (card-funded credit is spend-only)
    withdrawals_pending_micro: int
    estimated_monthly_need_micro: int
    reserve_required_micro: int
    min_topup_micro: int


class LedgerEntryOut(Out):
    tx_id: UUID
    kind: str
    memo: Optional[str] = None
    amount_micro: int                           # user perspective: + = balance increased
    created_at: datetime


class AmountIn(In):
    amount_micro: Optional[Micro] = None
    amount: Optional[UsdString] = None

    @model_validator(mode="after")
    def _amount(self) -> "AmountIn":
        micro = _one_amount(self.amount_micro, self.amount, "amount")
        self.amount_micro = micro
        self.amount = None
        return self


class StripeDepositIn(AmountIn):
    pass


class StripeDepositOut(Out):
    payment_intent_id: str
    client_secret: str
    amount_micro: int


class UsdcTypedDataIn(AmountIn):
    from_address: Address                       # a verified wallet of the user (the expected signer)
    signature_chain_id: ChainIdHex


class UsdcTypedDataOut(Out):
    from_address: str
    destination: str
    amount_micro: int
    time_ms: int
    payload: dict[str, Any]                     # {action, typed_data}
    exchange_url: str


class UsdcConfirmIn(In):
    from_address: Optional[Address] = None      # default: all verified wallets of the user
    time_ms: Optional[Annotated[StrictInt, Field(ge=1_600_000_000_000, le=4_000_000_000_000)]] = None


class DepositOut(Out):
    id: UUID
    method: str
    amount_micro: int
    status: str
    external_ref: Optional[str] = None
    created_at: datetime


class UsdcConfirmOut(Out):
    credited: list[DepositOut]
    fee_balance_micro: int


class WithdrawalIn(AmountIn):
    to_address: Address


class PayoutRequestIn(AmountIn):
    source: Literal["creator", "referrer"]
    to_address: Address


class PayoutOut(Out):
    id: UUID
    kind: str                                   # withdrawal | payout
    amount_micro: int
    to_address: str
    status: str
    tx_hash: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------------------------------------------- positions / alerts
class PositionOut(Out):
    trading_address: str
    coin: str
    size: str
    entry_px: Optional[str] = None
    position_value: Optional[str] = None
    unrealized_pnl: Optional[str] = None
    leverage: Optional[str] = None
    liquidation_px: Optional[str] = None


class PositionsOut(Out):
    positions: list[PositionOut]
    unavailable: list[str] = Field(default_factory=list)   # trading addresses that could not be fetched


class AlertOut(Out):
    id: UUID
    severity: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    acked_at: Optional[datetime] = None


# ---------------------------------------------------------------------------------------------------- reviews / posts
class ReviewIn(In):
    strategy_id: UUID
    rating: Annotated[StrictInt, Field(ge=1, le=5)]
    body: Optional[Annotated[str, StringConstraints(max_length=2000), AfterValidator(_clean_text)]] = None


class PostOut(Out):
    id: UUID
    title: str
    price_micro: int
    strategy_slug: Optional[str] = None
    published_at: Optional[datetime] = None
    body: Optional[str] = None                  # present only if free, purchased, or own
    purchased: bool = False


class PurchaseOut(Out):
    post_id: UUID
    charged_micro: int
    fee_balance_micro: int


# ---------------------------------------------------------------------------------------------------- referrals
class ReferralsOut(Out):
    code: str
    link: str
    tier: str
    share_of_pool_bps: int
    active_referred_users_30d: int
    referred_notional_30d_micro: int
    referred_users_total: int
    earnings_payable_micro: int
    earnings_total_micro: int
    next_tier: Optional[ReferralTierOut] = None


# ---------------------------------------------------------------------------------------------------- creator
class CreatorStrategyIn(In):
    slug: Slug
    name: Annotated[str, StringConstraints(min_length=3, max_length=64), AfterValidator(_clean_line)]
    description: Optional[Annotated[str, StringConstraints(max_length=5000), AfterValidator(_clean_text)]] = None
    markets: list[Coin] = Field(min_length=1, max_length=5)
    timeframe: Literal["1h", "4h", "1d"]
    price_monthly_micro: Optional[MicroOrZero] = None
    price_monthly: Optional[UsdString] = None
    profit_share_bps: Annotated[StrictInt, Field(ge=0, le=10_000)]

    @model_validator(mode="after")
    def _amount(self) -> "CreatorStrategyIn":
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("duplicate market")
        micro = _one_amount(self.price_monthly_micro, self.price_monthly, "price_monthly", allow_zero=True)
        self.price_monthly_micro = micro
        self.price_monthly = None
        return self


class CreatorStrategyPatchIn(In):
    name: Optional[Annotated[str, StringConstraints(min_length=3, max_length=64), AfterValidator(_clean_line)]] = None
    description: Optional[Annotated[str, StringConstraints(max_length=5000), AfterValidator(_clean_text)]] = None
    price_monthly_micro: Optional[MicroOrZero] = None
    price_monthly: Optional[UsdString] = None
    profit_share_bps: Optional[Annotated[StrictInt, Field(ge=0, le=10_000)]] = None

    @model_validator(mode="after")
    def _amount(self) -> "CreatorStrategyPatchIn":
        micro = _one_amount(self.price_monthly_micro, self.price_monthly, "price_monthly", allow_zero=True,
                            required=False)
        self.price_monthly_micro = micro
        self.price_monthly = None
        return self


MAX_CODE_BYTES = 64 * 1024


class VersionUploadIn(In):
    source: Literal["python", "nocode"]
    code: Optional[Annotated[str, StringConstraints(min_length=1, max_length=MAX_CODE_BYTES)]] = None
    spec: Optional[dict[str, Any]] = None

    @model_validator(mode="after")
    def _one_source(self) -> "VersionUploadIn":
        if self.source == "python":
            if not self.code or self.spec is not None:
                raise ValueError("python upload needs `code` only")
            if len(self.code.encode("utf-8")) > MAX_CODE_BYTES:
                raise ValueError("code exceeds 64 KB")
        else:
            if self.spec is None or self.code is not None:
                raise ValueError("nocode upload needs `spec` only")
        return self


class CreatorVersionOut(Out):
    id: UUID
    version: int
    code_hash: str
    published_at: Optional[datetime] = None
    live_since: Optional[datetime] = None
    params: dict[str, Any] = Field(default_factory=dict)
    backtest: Optional[dict[str, Any]] = None
    created_at: datetime
    warning: Optional[str] = None


class CreatorStrategyOut(Out):
    id: UUID
    slug: str
    name: str
    status: str
    markets: list[str]
    timeframe: str
    price_monthly_micro: Optional[int]
    profit_share_bps: int
    description: Optional[str] = None
    created_at: datetime


class CreatorPostIn(In):
    title: Title
    body: Annotated[str, StringConstraints(min_length=1, max_length=50_000), AfterValidator(_clean_text)]
    strategy_id: Optional[UUID] = None
    price_micro: Optional[MicroOrZero] = None
    price: Optional[UsdString] = None

    @model_validator(mode="after")
    def _amount(self) -> "CreatorPostIn":
        micro = _one_amount(self.price_micro, self.price, "price", allow_zero=True)
        self.price_micro = micro
        self.price = None
        return self


class StrategyEarningsOut(Out):
    strategy_id: UUID
    slug: str
    active_subscribers: int
    earned_micro: Optional[int] = None          # not broken down per strategy yet (see total_earned_micro)


class EarningsOut(Out):
    payable_micro: int
    payouts_pending_micro: int
    total_earned_micro: int
    by_strategy: list[StrategyEarningsOut]
    recent: list[LedgerEntryOut]


class KycSessionOut(Out):
    url: str                                    # "" when manual
    provider: str
    status: str
    manual: bool = False                        # manual review by our team: no redirect (web shows a notice)


# ---------------------------------------------------------------------------------------------------- admin
# Global switches, or per-market `kill_switch_market:{coin}` / `new_entries_paused:{coin}`; coin may carry a builder
# dex prefix with its own ':' (e.g. new_entries_paused:xyz:SILVER).
FLAG_KEY_PATTERN = (r"^(?:kill_switch_global|new_entries_paused"
                    r"|(?:kill_switch_market|new_entries_paused):(?:[a-z0-9]{1,16}:)?[A-Za-z0-9]{1,32})$")


class FlagOut(Out):
    key: str
    value: Any
    pending_value: Any = None                   # proposed by pending_by; a DIFFERENT admin must approve
    pending_by: Optional[str] = None
    pending_at: Optional[datetime] = None
    updated_by: Optional[str] = None
    updated_at: Optional[datetime] = None


class FlagSetIn(In):
    key: Annotated[str, StringConstraints(pattern=FLAG_KEY_PATTERN)]
    value: StrictBool
    reason: Reason


class ChangeOut(Out):
    id: UUID
    kind: str
    target: str
    payload: dict[str, Any]
    reason: str
    status: str
    maker_admin: UUID
    checker_admin: Optional[UUID] = None
    created_at: datetime
    decided_at: Optional[datetime] = None


class AdminActionOut(Out):
    status: Literal["applied", "pending"]
    change: Optional[ChangeOut] = None


class DecisionIn(In):
    reason: Reason


class AdminPayoutOut(Out):
    id: UUID
    kind: str
    beneficiary: UUID
    amount_micro: int
    to_address: str
    status: str
    maker_admin: Optional[UUID] = None
    checker_admin: Optional[UUID] = None
    tx_hash: Optional[str] = None
    created_at: datetime


class PayoutTypedDataIn(In):
    signature_chain_id: ChainIdHex


class PayoutTypedDataOut(Out):
    payout: AdminPayoutOut
    payload: dict[str, Any]
    exchange_url: str


class PayoutSentIn(In):
    tx_hash: TxHash
    time_ms: Annotated[StrictInt, Field(ge=1_600_000_000_000, le=4_000_000_000_000)]


class AdminStrategyOut(Out):
    id: UUID
    slug: str
    name: str
    status: str
    in_house: bool
    owner_user_id: Optional[UUID] = None
    owner_kyc_status: Optional[str] = None
    price_monthly_micro: Optional[int] = None
    profit_share_bps: int
    versions: list[CreatorVersionOut] = Field(default_factory=list)


class KycDecisionIn(In):
    decision: Literal["approved", "rejected"]
    reason: Reason


class StrategyListIn(In):
    version_id: UUID
    reason: Reason


class PriceSetIn(In):
    price_monthly_micro: Optional[MicroOrZero] = None
    price_monthly: Optional[UsdString] = None
    reason: Reason

    @model_validator(mode="after")
    def _amount(self) -> "PriceSetIn":
        micro = _one_amount(self.price_monthly_micro, self.price_monthly, "price_monthly", allow_zero=True)
        self.price_monthly_micro = micro
        self.price_monthly = None
        return self


class AdminUserOut(Out):
    id: UUID
    email: Optional[str]
    display_name: Optional[str]
    role: str
    plan: str
    status: str
    country_attested: Optional[str] = None
    created_at: datetime


class ReconciliationOut(Out):
    report: Optional[dict[str, Any]] = None
    generated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------------------------------- internal
class SettleIn(In):
    settle_date: Optional[date] = None


class JobOut(Out):
    job: str
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
