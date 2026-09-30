"""Ports (small Protocols) the execution package depends on.

Execution code never imports the DB, the Hyperliquid SDK, KMS or the domain modules directly: it talks to these
interfaces and the lead wires concrete adapters (see ``app.execution.wiring`` for adapters over ``app.domain``).
Every value crossing a port is a frozen dataclass defined here, so fakes in tests and real adapters agree on shape.

Conventions
- Money: integer micro-USD (``app.money``). Sizes / prices: ``Decimal`` (never float).
- Time: timezone-aware UTC ``datetime``.
- Signed notionals: + long / − short.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------------------------------------------------

# Subscription statuses (SPEC §4). Only these are ever traded by the executor on strategy signals.
TRADABLE_STATUSES = ("active", "past_due", "reduce_only")
# SPEC §12 cancel flow: "close" → status ``closing``: the executor flattens every strategy market reduce-only (target
# weight 0, whatever the signals say) until the on-chain positions are 0, then sets ``cancelled``. "leave" →
# ``cancelled`` at once: the executor never touches the account again for that subscription.
CLOSING_STATUS = "closing"
# BarSignal.source of the synthetic all-zero signal the executor builds for a closing subscription.
CLOSING_SOURCE = "closing"


@dataclass(frozen=True)
class SubscriptionView:
    id: str
    user_id: str
    strategy_id: str
    strategy_version_id: str
    master_address: str             # wallet that approved our agent (lower-case)
    trading_address: str            # master or sub-account (lower-case); orders use vault_address when ≠ master
    allocation_micro: int
    max_leverage_x100: int
    status: str                     # pending|active|past_due|reduce_only|paused_user|closing|cancelled
    markets: tuple[str, ...]        # strategy market whitelist (e.g. ("xyz:SILVER",))
    consecutive_rejections: int = 0  # circuit-breaker counter
    strategy_max_leverage_x100: int | None = None
    past_due_since: datetime | None = None
    # SPEC §12 alert contacts: False when the user has no confirmed email or no working Telegram link (past the
    # 24 h grace) — SQL ``alert_contacts_entries_allowed(user, now)``. The executor and the planner then treat the
    # subscription as ``reduce_only`` for this tick: exits still run, no new or increased exposure.
    entries_allowed: bool = True


@dataclass(frozen=True)
class BarSignal:
    """All target weights of one strategy version for one bar close (from the ``signals`` table)."""
    strategy_version_id: str
    bar_close: datetime
    weights_bps: Mapping[str, int]  # coin -> weight × 10000 (signed)
    source: str = "terminal"        # sandbox|terminal (|closing: synthetic, built by the executor)


@dataclass(frozen=True)
class MarketSnapshot:
    coin: str
    mid_px: Decimal
    mark_px: Decimal
    oracle_px: Decimal
    day_notional_volume_micro: int
    open_interest_notional_micro: int
    max_leverage: int
    sz_decimals: int
    as_of: datetime
    is_delisted: bool = False


@dataclass(frozen=True)
class Position:
    coin: str
    szi: Decimal                    # signed size (+ long / − short); 0 when flat
    notional_micro: int             # signed position notional at mark

    @staticmethod
    def flat(coin: str) -> "Position":
        return Position(coin=coin, szi=Decimal(0), notional_micro=0)


@dataclass(frozen=True)
class PlanInput:
    subscription: SubscriptionView
    coin: str
    weight_bps: int                 # target weight × 10000 (signed)
    position: Position
    snapshot: MarketSnapshot
    flags: "Flags"
    reduce_only_mode: bool          # status reduce_only or new entries paused: exposure may only shrink
    now: datetime


@dataclass(frozen=True)
class OrderLeg:
    is_buy: bool
    sz: Decimal                     # already rounded to szDecimals
    limit_px: Decimal               # already on the price grid, within the slippage cap
    reduce_only: bool
    close_position: bool = False    # full close: the executor sizes it from the exact on-chain |szi|
    notional_micro: int = 0         # > 0, planned notional of this leg


@dataclass(frozen=True)
class OrderPlan:
    """Planner output for one coin: zero legs = nothing to do (already at target within the rebalance threshold).
    A flip is two legs: a reduce-only close, then the opening leg."""
    coin: str
    target_notional_micro: int      # position notional the legs aim for (for reconcile)
    legs: tuple[OrderLeg, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def action(self) -> str:
        return "order" if self.legs else "skip"


@dataclass(frozen=True)
class PlaceResult:
    """Normalised exchange response for one IOC order."""
    status: str                     # filled | partial | rejected | resting | unknown
    filled_sz: Decimal = Decimal(0)
    avg_px: Decimal | None = None
    oid: int | None = None
    error: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


# Order row statuses written by the executor.
ORDER_SUBMITTING = "submitting"     # intent recorded, exchange outcome not yet known (crash-safe marker)
ORDER_FILLED = "filled"
ORDER_PARTIAL = "partial"
ORDER_REJECTED = "rejected"
ORDER_RESTING = "resting"
ORDER_NOT_SUBMITTED = "not_submitted"  # exchange has never seen this cloid (resolved after a crash)
ORDER_UNKNOWN = "unknown"
PENDING_ORDER_STATUSES = (ORDER_SUBMITTING, ORDER_UNKNOWN)


@dataclass(frozen=True)
class OrderRecord:
    subscription_id: str
    strategy_version_id: str
    bar_close: datetime
    attempt: int                    # 0-based attempt number within (subscription, bar, coin)
    cloid: str                      # deterministic (executor.make_cloid → app.hl.client.make_cloid)
    coin: str
    is_buy: bool
    sz: Decimal
    limit_px: Decimal
    reduce_only: bool
    status: str
    jitter_seconds: int
    submitted_at: datetime
    filled_sz: Decimal = Decimal(0)
    avg_px: Decimal | None = None
    oid: int | None = None
    error: str | None = None
    hl_response: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertEvent:
    severity: str                   # info | warn | critical
    kind: str
    payload: Mapping[str, Any]
    user_id: str | None = None
    dedup_key: str | None = None    # sinks may drop repeats with the same key
    coin: str | None = None         # critical + coin → notifier auto-pauses new entries on that market


@dataclass(frozen=True)
class Flags:
    """Snapshot of ``system_flags`` taken once per tick."""
    kill_switch_global: bool = False
    new_entries_paused: bool = False
    killed_markets: frozenset[str] = frozenset()        # kill_switch_market:{coin}  → no orders on coin
    paused_entry_markets: frozenset[str] = frozenset()  # new_entries_paused:{coin} → reduce-only on coin


@dataclass(frozen=True)
class ExpectedPosition:
    subscription_id: str
    user_id: str
    trading_address: str
    coin: str
    target_notional_micro: int
    allocation_micro: int
    bar_close: datetime


# --- settlement -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SettlementSubscription:
    id: str
    user_id: str
    strategy_id: str
    creator_user_id: str | None     # None for in-house (creator share → platform)
    in_house: bool
    status: str
    profit_share_bps: int           # creator rate 0–1500
    price_monthly_micro: int
    cum_pnl_micro: int
    hwm_micro: int
    pnl_cursor: datetime | None     # attributed PnL settled up to (exclusive); None → since subscription start
    current_period_end: datetime | None
    past_due_since: datetime | None
    created_at: datetime
    trading_address: str | None = None   # the account whose fills/funding feed this subscription's PnL
    cancelled_at: datetime | None = None


@dataclass(frozen=True)
class DataCoverage:
    """How far the data jobs have COMPLETELY synced one trading address (ms since epoch, None = never).

    ``fills_ms`` / ``funding_ms``: the newest time up to which ``fills-ingest`` / ``funding-scan`` have fetched
    everything for the address (a complete run → its run time; an incomplete one → its monotonic cursor)."""
    fills_ms: int | None
    funding_ms: int | None


@dataclass(frozen=True)
class PnlDelta:
    realized_micro: int             # Σ (closedPnl − fee) of attributed fills in (since, until]
    funding_micro: int              # Σ funding while holding strategy coins
    until: datetime

    @property
    def total_micro(self) -> int:
        return self.realized_micro + self.funding_micro


@dataclass(frozen=True)
class ProfitShareCharge:
    creator_micro: int
    platform_micro: int
    new_cum_pnl_micro: int
    new_hwm_micro: int
    profit_micro: int

    @property
    def total_micro(self) -> int:
        return self.creator_micro + self.platform_micro


@dataclass(frozen=True)
class BuilderFeeFill:
    """One fill whose builder fee is not yet recognised. A Hyperliquid ``tid`` identifies the TRADE (shared by both
    counterparties), so a fill is identified by ``(trading_address, tid)`` (fills UNIQUE constraint)."""
    tid: str
    subscription_id: str | None
    user_id: str | None
    creator_user_id: str | None
    in_house: bool
    builder_fee_micro: int
    time: datetime
    trading_address: str


@dataclass(frozen=True)
class BuilderFeeSplit:
    creator_micro: int
    platform_micro: int
    referrer_micro: int

    @property
    def total_micro(self) -> int:
        return self.creator_micro + self.platform_micro + self.referrer_micro


@dataclass(frozen=True)
class PlanAccount:
    user_id: str
    plan: str                       # free|pro|max
    price_monthly_micro: int
    plan_period_end: datetime | None
    past_due_since: datetime | None
    anchor: datetime | None = None  # plan start (renewals computed from it to avoid month-end drift)


@dataclass(frozen=True)
class LedgerLine:
    account_code: str               # e.g. "user:{id}:fee_balance"
    amount_micro: int               # + debit / − credit; Σ over a transaction == 0


# ---------------------------------------------------------------------------------------------------------------------
# Ports: execution
# ---------------------------------------------------------------------------------------------------------------------

@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class SignalRepo(Protocol):
    def latest_signals(self) -> Sequence[BarSignal]:
        """Latest bar_close signal per *listed* strategy version (one BarSignal per version)."""


class SubscriptionRepo(Protocol):
    def due_subscriptions(self, strategy_version_id: str, bar_close: datetime, limit: int) -> Sequence[SubscriptionView]:
        """Subscriptions of the version in TRADABLE_STATUSES whose bar ``bar_close`` is not yet marked done."""

    def is_bar_done(self, subscription_id: str, bar_close: datetime) -> bool: ...

    def mark_bar_done(self, subscription_id: str, bar_close: datetime, outcome: str) -> None:
        """Idempotent. outcome ∈ {"ok", "residual", "skipped_reduce_only", ...}."""

    def orders_for_bar(self, subscription_id: str, bar_close: datetime, coin: str) -> Sequence[OrderRecord]:
        """Orders already recorded for (subscription, bar, coin), ordered by attempt."""

    def get_order(self, cloid: str) -> OrderRecord | None: ...

    def get_subscription(self, subscription_id: str) -> SubscriptionView | None:
        """Fresh view (any status). The executor re-reads under the advisory lock, so a cancel ("leave") or a
        switch to ``closing`` that lands while a tick is running is honoured before any order is sent."""

    def closing_subscriptions(self, limit: int) -> Sequence[SubscriptionView]:
        """Subscriptions in status ``closing`` (SPEC §12), independent of any signal."""

    def unresolved_orders(self, subscription_id: str) -> Sequence[OrderRecord]:
        """Orders of the subscription (any bar) whose outcome is unknown (submitting | unknown | resting)."""

    def finish_closing(self, subscription_id: str, now: datetime) -> bool:
        """closing → cancelled (cancelled_at = now). Atomic on the current status; False if it was not closing."""

    def insert_order(self, record: OrderRecord) -> bool:
        """Insert the order *intent* (status ``submitting``) BEFORE sending. Must be atomic on the unique cloid:
        returns False (and changes nothing) if a row with that cloid already exists."""

    def update_order(self, cloid: str, *, status: str, filled_sz: Decimal, avg_px: Decimal | None, oid: int | None,
                     error: str | None, hl_response: Mapping[str, Any]) -> None: ...

    def record_rejection(self, subscription_id: str, reason: str) -> int:
        """Increment and return the consecutive-rejection counter."""

    def reset_rejections(self, subscription_id: str) -> None: ...

    def record_target(self, subscription_id: str, coin: str, bar_close: datetime, target_notional_micro: int,
                      weight_bps: int) -> None:
        """Remember the last target (reconcile compares on-chain positions with it)."""


class MarketData(Protocol):
    def snapshot(self, coin: str) -> MarketSnapshot | None:
        """Fresh mid/mark/oracle/volume/OI; None when unavailable. May raise ExternalServiceError."""


class PositionReader(Protocol):
    def positions(self, address: str, coins: Iterable[str]) -> Mapping[str, Position]:
        """Signed positions from clearinghouseState (incl. builder dexes); missing coin ⇒ flat."""


class OrderStatusReader(Protocol):
    """Info endpoint (no key needed): resolve an order we may or may not have sent."""

    def order_status_by_cloid(self, address: str, cloid: str) -> PlaceResult | None:
        """None ⇒ the exchange does not know this cloid (never accepted)."""


class ExchangeGateway(Protocol):
    def place_ioc(self, *, coin: str, is_buy: bool, sz: Decimal, limit_px: Decimal, reduce_only: bool,
                  cloid: str) -> PlaceResult:
        """Place one IOC limit order. The gateway MUST attach our builder code {b, f} to every order."""


class GatewayFactory(Protocol):
    def create(self, key: Any, account_address: str, vault_address: str | None) -> ExchangeGateway: ...


class KeyProvider(Protocol):
    def agent_key(self, user_id: str, master_address: str) -> AbstractContextManager[Any]:
        """Yield the decrypted agent key (opaque to us); the key material is zeroized on exit."""


class LockProvider(Protocol):
    def try_lock(self, key: str) -> AbstractContextManager[bool]:
        """Non-blocking advisory lock (pg_try_advisory_lock). Yields True when held; released on exit."""


class FlagRepo(Protocol):
    def flags(self) -> Flags: ...


class AlertSink(Protocol):
    def emit(self, alert: AlertEvent) -> None: ...


class OrderPlanner(Protocol):
    def plan(self, inp: PlanInput) -> OrderPlan:
        """Pure pre-trade planning (wraps ``app.domain.risk.plan_order``). Raises ``GuardRejected`` with
        ``details={"reasons": (...), "scope": "market"|"subscription"}``. Guard rejections never count towards the
        circuit breaker (only exchange rejections and unexpected errors do); ``scope`` only shapes alerts."""


class Jitter(Protocol):
    def delay_seconds(self, user_id: str, bar_close: datetime) -> int:
        """Per-user deterministic delay (seconds) after bar_close (domain.jitter.delay_seconds)."""

    def fair_order(self, subscription_ids: Sequence[str], bar_close: datetime) -> list[str]:
        """Deterministic per-bar shuffle (domain.jitter.fair_order)."""


# ---------------------------------------------------------------------------------------------------------------------
# Ports: reconcile
# ---------------------------------------------------------------------------------------------------------------------

class ReconcileRepo(Protocol):
    def expected_positions(self) -> Sequence[ExpectedPosition]:
        """Last recorded target per (subscription, coin) for tradable subscriptions whose bar is marked done."""

    def total_builder_fees_micro(self) -> int:
        """Σ fills.builder_fee_micro over all fills (all time)."""


class BuilderRewardsReader(Protocol):
    def cumulative_builder_rewards_micro(self) -> int:
        """Builder fees accrued on-chain to our builder address (all time, claimed + unclaimed)."""


class TreasuryReader(Protocol):
    def treasury_usdc_micro(self) -> int: ...


class LedgerReader(Protocol):
    def balance(self, account_code: str) -> int:
        """Signed balance (+debit / −credit)."""


# ---------------------------------------------------------------------------------------------------------------------
# Ports: settlement
# ---------------------------------------------------------------------------------------------------------------------

class LedgerPoster(Protocol):
    def post_transaction(self, *, idempotency_key: str, kind: str, memo: str, lines: Sequence[LedgerLine],
                         created_by: str) -> tuple[str, bool]:
        """Post a balanced transaction. Idempotent on key: returns (tx_id, created). A repeat with the same key
        returns the existing tx id and created=False (adapter may raise Conflict if lines differ)."""

    def has_transaction(self, idempotency_key: str) -> bool: ...

    def balance(self, account_code: str) -> int: ...


class UnitOfWork(Protocol):
    def atomic(self) -> AbstractContextManager[None]:
        """One DB transaction: ledger post + subscription update commit together or not at all."""


class SettlementRepo(Protocol):
    def subscriptions_to_settle(self) -> Sequence[SettlementSubscription]:
        """Every subscription that may carry PnL (incl. cancelled with an unsettled cursor)."""

    def is_settled(self, subscription_id: str, settle_date: date) -> bool:
        """True when the (subscription, settle_date) settlement row exists."""

    def pnl_since(self, subscription_id: str, since: datetime | None, until: datetime) -> PnlDelta:
        """Attributed fills (closedPnl − fee) + funding in (since, until]."""

    def data_coverage(self, trading_addresses: Sequence[str]) -> Mapping[str, DataCoverage]:
        """Per trading address: how far fills-ingest and funding-scan have completely synced (job_cursors).
        Addresses never synced are absent (or carry None). Settlement defers a subscription until both have
        passed its PnL cut-off (SPEC §1.1: a fill stored after its day was settled is never counted)."""

    def save_profit_share(self, subscription_id: str, settle_date: date, *, cum_pnl_micro: int, hwm_micro: int,
                          pnl_cursor: datetime, ledger_tx_id: str | None) -> None: ...

    def set_status(self, subscription_id: str, status: str, past_due_since: datetime | None) -> None: ...

    def set_period_end(self, subscription_id: str, period_end: datetime) -> None: ...

    def unrecognised_builder_fee_fills(self, until: datetime, limit: int) -> Sequence[BuilderFeeFill]: ...

    def mark_builder_fee_recognised(self, trading_address: str, tid: str, ledger_tx_id: str) -> None:
        """Mark the fill ``(trading_address, tid)`` recognised; ``ledger_tx_id`` "" = nothing was posted."""

    def plans_due(self, now: datetime) -> Sequence[PlanAccount]:
        """Paid plans (pro/max) whose plan_period_end ≤ now."""

    def set_plan_period(self, user_id: str, plan_period_end: datetime | None, past_due_since: datetime | None) -> None: ...

    def downgrade_plan(self, user_id: str, plan: str) -> None: ...


class UserEventSink(Protocol):
    """User-facing events written in the CALLER'S transaction (settlement: inside ``UnitOfWork.atomic()``, right after
    the ledger post they describe). Delivered to Telegram / email / in-app by ``app.alerts.delivery``."""

    def emit(self, *, user_id: str, kind: str, severity: str, payload: Mapping[str, Any], dedup_key: str) -> bool:
        """One user event (kinds: ``app.alerts.prefs.CATALOG``; payload: ``app.alerts.user_templates``). Idempotent
        on ``dedup_key``; returns False when it already existed."""

    def fee_balance_changed(self, *, user_id: str, prev_micro: int, new_micro: int, now: datetime) -> list[str]:
        """Low-balance hook (``app.alerts.delivery.on_balance_changed``): user-facing balances (−ledger) before and
        after a posting on ``user:{id}:fee_balance``. Never raises; returns the emitted kinds."""


class ReferralLookup(Protocol):
    def referrer_share(self, user_id: str) -> tuple[str, int] | None:
        """(referrer_user_id, share_of_pool_bps) for the user's current tier, or None when not referred."""


class ProfitShareCalculator(Protocol):
    def settle(self, *, cum_pnl_micro: int, hwm_micro: int, pnl_delta_micro: int, creator_bps: int,
               in_house: bool) -> ProfitShareCharge: ...


class FeeSplitter(Protocol):
    def split_builder_fee(self, fee_micro: int, *, in_house: bool, referrer_share_bps: int | None) -> BuilderFeeSplit:
        """Exact split of a collected builder fee (domain.fees.split_builder_fee)."""

    def split_subscription(self, price_micro: int) -> tuple[int, int]:
        """(creator_micro, platform_micro); sums exactly to price."""


class BillingPolicy(Protocol):
    def next_status(self, *, status: str, balance_micro: int, amount_due_micro: int, past_due_since: datetime | None,
                    now: datetime) -> tuple[str, datetime | None]:
        """Fee-balance state machine (domain.billing.next_status): returns (status, past_due_since). ``active``
        with amount_due > 0 means the balance covers it and the caller deducts it."""

    def next_period_end(self, anchor: datetime, now: datetime) -> datetime:
        """First monthly renewal instant from ``anchor`` strictly after ``now`` (domain.billing.next_renewal_after)."""

    @property
    def grace_hours(self) -> int: ...
