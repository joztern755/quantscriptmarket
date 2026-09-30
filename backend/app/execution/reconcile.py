"""Daily reconciliation (SPEC §5.5, §5.6; ``/internal/reconcile``).

(a) positions: last executed target per (subscription, coin) vs the on-chain position → drift alerts.
(b) builder fees: Σ builder fees of fills in our DB vs builder rewards accrued on-chain → critical alert if > $1.
(c) treasury: ledger balance of ``treasury:hl_usdc`` vs the treasury's on-chain USDC → critical alert if > $1.
(d) solvency (REVIEW_MONEY M7(d)): on-chain treasury USDC + builder receivable + Stripe clearing (ledger) must cover
    every liability: Σ positive user fee balances + creator/referrer payables + uncollected profit share (ps_pending,
    conservatively) + withdrawals/payouts/refunds pending + suspense. Negative fee balances (user debt) are NOT counted
    as assets (receivables of doubtful value, C1). Shortfall → critical alert.
(e) Stripe clearing (REVIEW_MONEY M7(c)): when a ``StripeClearingReader`` is wired (executor has a Stripe key), every
    Stripe payout balance transaction since the last run is booked first (``stripe_payout``: bank:payouts [+ payout
    fee → expense:stripe_fees] ← stripe:clearing, key ``stripe:payout:{txn}``; a failed / cancelled payout books the
    reverse, ``stripe_payout_reversal``), then ledger ``stripe:clearing`` is compared with Stripe available + pending.
    No reader → ``stripe_clearing_status = "not_configured"``; a non-USD settlement currency → ``"unsupported_currency"``
    (nothing booked; see app.execution.treasury_books).
(f) builder receivable (REVIEW_MONEY M7(b)): when a ``BuilderClaimsReader`` is wired, builder rewards claimed into the
    treasury since the last run are booked first (``builder_rewards_claim``: treasury:hl_usdc ← builder:hl_receivable,
    key ``builder_claim:{hash}:{time}``; only when the builder address IS the treasury, else ``builder_claims_status =
    "outside_treasury"``), then ledger receivable + builder fees of our fills not yet recognised by settlement is
    compared with the rewards still claimable on-chain → critical alert if > $1.
The treasury check (c) runs after both bookings, so it sees them; its reader sums every treasury balance (perp, spot
USDC, each trusted builder dex — M7(g)) and the report carries the breakdown.

Every check is isolated: one failing (e.g. an info endpoint outage) is reported and the others still run.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from app.logging import get_logger
from app.money import BPS, usd

from .ports import (
    AlertEvent,
    AlertSink,
    BuilderClaimsReader,
    BuilderRewardsReader,
    LedgerLine,
    LedgerPoster,
    LedgerReader,
    PositionReader,
    ReconcileRepo,
    SolvencyRepo,
    StripeClearingReader,
    TreasuryReader,
    UnitOfWork,
)

log = get_logger("app.execution.reconcile")

TREASURY_ACCOUNT = "treasury:hl_usdc"
BUILDER_RECEIVABLE = "builder:hl_receivable"
STRIPE_CLEARING = "stripe:clearing"
BANK_PAYOUTS = "bank:payouts"
EXPENSE_STRIPE_FEES = "expense:stripe_fees"
CREATED_BY = "system:reconcile"
CURSOR_OVERLAP_MS = 3_600_000                  # re-read the last hour: bookings are idempotent on their keys


@dataclass(frozen=True)
class ReconcileConfig:
    mismatch_threshold_micro: int = usd(1)     # SPEC §5.5: ledger ↔ on-chain mismatch > $1
    drift_min_micro: int = usd(10)             # ignore drift below the minimum order size
    drift_pct_of_allocation_bps: int = 1000    # daily bars: price moves shift notional; alert above 10% of allocation


@dataclass
class Drift:
    subscription_id: str
    coin: str
    expected_micro: int
    actual_micro: int

    @property
    def drift_micro(self) -> int:
        return self.actual_micro - self.expected_micro


@dataclass
class ReconcileReport:
    positions_checked: int = 0
    drifts: list[Drift] = field(default_factory=list)
    builder_db_micro: int | None = None
    builder_chain_micro: int | None = None
    builder_mismatch: bool = False
    treasury_ledger_micro: int | None = None
    treasury_chain_micro: int | None = None
    treasury_mismatch: bool = False
    solvency: dict[str, int] | None = None
    solvency_shortfall_micro: int | None = None
    stripe_clearing_status: str = "not_configured"
    stripe_clearing_ledger_micro: int | None = None
    stripe_clearing_stripe_micro: int | None = None
    stripe_payouts_booked: int = 0
    stripe_payouts_booked_micro: int = 0
    treasury_breakdown: dict[str, int] | None = None
    builder_claims_status: str = "not_configured"
    builder_claims_booked: int = 0
    builder_claims_booked_micro: int = 0
    builder_receivable_ledger_micro: int | None = None
    builder_unrecognised_micro: int | None = None
    builder_unclaimed_chain_micro: int | None = None
    builder_receivable_mismatch: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "positions_checked": self.positions_checked,
            "drifts": [{"subscription_id": d.subscription_id, "coin": d.coin, "expected_micro": d.expected_micro,
                        "actual_micro": d.actual_micro} for d in self.drifts],
            "builder_db_micro": self.builder_db_micro, "builder_chain_micro": self.builder_chain_micro,
            "builder_mismatch": self.builder_mismatch,
            "treasury_ledger_micro": self.treasury_ledger_micro, "treasury_chain_micro": self.treasury_chain_micro,
            "treasury_mismatch": self.treasury_mismatch,
            "solvency": dict(self.solvency) if self.solvency is not None else None,
            "solvency_shortfall_micro": self.solvency_shortfall_micro,
            "stripe_clearing_status": self.stripe_clearing_status,
            "stripe_clearing_ledger_micro": self.stripe_clearing_ledger_micro,
            "stripe_clearing_stripe_micro": self.stripe_clearing_stripe_micro,
            "stripe_payouts_booked": self.stripe_payouts_booked,
            "stripe_payouts_booked_micro": self.stripe_payouts_booked_micro,
            "treasury_breakdown": dict(self.treasury_breakdown) if self.treasury_breakdown is not None else None,
            "builder_claims_status": self.builder_claims_status,
            "builder_claims_booked": self.builder_claims_booked,
            "builder_claims_booked_micro": self.builder_claims_booked_micro,
            "builder_receivable_ledger_micro": self.builder_receivable_ledger_micro,
            "builder_unrecognised_micro": self.builder_unrecognised_micro,
            "builder_unclaimed_chain_micro": self.builder_unclaimed_chain_micro,
            "builder_receivable_mismatch": self.builder_receivable_mismatch,
            "errors": list(self.errors),
        }


class Reconciler:
    def __init__(self, *, repo: ReconcileRepo, positions: PositionReader, builder_rewards: BuilderRewardsReader,
                 treasury: TreasuryReader, ledger: LedgerReader, alerts: AlertSink,
                 config: ReconcileConfig | None = None, solvency: SolvencyRepo | None = None,
                 stripe: StripeClearingReader | None = None, builder_claims: BuilderClaimsReader | None = None,
                 poster: LedgerPoster | None = None, uow: UnitOfWork | None = None) -> None:
        self.repo = repo
        self.positions = positions
        self.builder_rewards = builder_rewards
        self.treasury = treasury
        self.ledger = ledger
        self.alerts = alerts
        self.cfg = config or ReconcileConfig()
        self.solvency = solvency if solvency is not None else (repo if hasattr(repo, "solvency_ledger") else None)
        self.stripe = stripe
        self.builder_claims = builder_claims
        # bookings (M7(b)/(c)) need a poster: the ledger adapter itself when it can post (PgLedger)
        self.poster = poster if poster is not None else (ledger if hasattr(ledger, "post_transaction") else None)
        self.uow = uow

    def run(self, date_key: str) -> ReconcileReport:
        """``date_key`` (e.g. "2026-09-30") scopes alert dedup keys to one run per day."""
        report = ReconcileReport()
        checks = [("positions", self.check_positions), ("builder_fees", self.check_builder_fees)]
        if self.builder_claims is not None:          # book first: the treasury and receivable checks must see them
            checks += [("builder_claims", self.book_builder_claims),
                       ("builder_receivable", self.check_builder_receivable)]
        if self.stripe is not None:
            checks.append(("stripe_payouts", self.book_stripe_payouts))
        checks.append(("treasury", self.check_treasury))
        if self.solvency is not None:
            checks.append(("solvency", self.check_solvency))
        if self.stripe is not None:
            checks.append(("stripe_clearing", self.check_stripe_clearing))
        for name, fn in checks:
            try:
                fn(report, date_key)
            except Exception as exc:
                report.errors.append(f"{name}:{type(exc).__name__}")
                log.error("reconcile_check_failed", exc_info=True, extra={"fields": {"check": name}})
                self._alert("warn", "reconcile_check_failed", {"check": name, "error": type(exc).__name__},
                            dedup=f"reconcile_failed:{name}:{date_key}")
        log.info("reconcile_done", extra={"fields": report.as_dict()})
        return report

    # (a) -------------------------------------------------------------------------------------------------------------
    def check_positions(self, report: ReconcileReport, date_key: str) -> None:
        by_address: dict[str, list] = {}
        for e in self.repo.expected_positions():
            by_address.setdefault(e.trading_address, []).append(e)
        for address, expected in by_address.items():
            try:
                actual = self.positions.positions(address, [e.coin for e in expected])
            except Exception as exc:
                report.errors.append(f"positions:{type(exc).__name__}")
                continue
            for e in expected:
                report.positions_checked += 1
                pos = actual.get(e.coin)
                actual_micro = pos.notional_micro if pos is not None else 0
                tol = max(self.cfg.drift_min_micro, e.allocation_micro * self.cfg.drift_pct_of_allocation_bps // BPS)
                sign_flip = (actual_micro > 0 > e.target_notional_micro) or (actual_micro < 0 < e.target_notional_micro)
                if abs(actual_micro - e.target_notional_micro) > tol or sign_flip:
                    d = Drift(e.subscription_id, e.coin, e.target_notional_micro, actual_micro)
                    report.drifts.append(d)
                    self._alert("warn", "position_drift", {
                        "subscription_id": e.subscription_id, "coin": e.coin,
                        "expected_micro": e.target_notional_micro, "actual_micro": actual_micro,
                        "bar_close": e.bar_close.isoformat()},
                        dedup=f"drift:{e.subscription_id}:{e.coin}:{date_key}")

    # (b) -------------------------------------------------------------------------------------------------------------
    def check_builder_fees(self, report: ReconcileReport, date_key: str) -> None:
        db = int(self.repo.total_builder_fees_micro())
        chain = int(self.builder_rewards.cumulative_builder_rewards_micro())
        report.builder_db_micro, report.builder_chain_micro = db, chain
        if abs(db - chain) > self.cfg.mismatch_threshold_micro:
            report.builder_mismatch = True
            self._alert("critical", "reconciliation_mismatch", {"scope": "builder fees", "ledger_micro": db,
                                                                "onchain_micro": chain, "diff_micro": chain - db},
                        dedup=f"builder_mismatch:{date_key}")

    # (c) -------------------------------------------------------------------------------------------------------------
    def check_treasury(self, report: ReconcileReport, date_key: str) -> None:
        ledger = int(self.ledger.balance(TREASURY_ACCOUNT))  # asset: debit-positive
        chain = int(self.treasury.treasury_usdc_micro())
        report.treasury_ledger_micro, report.treasury_chain_micro = ledger, chain
        breakdown = getattr(self.treasury, "last_breakdown", None)
        if isinstance(breakdown, dict) and breakdown:
            report.treasury_breakdown = {str(k): int(v) for k, v in breakdown.items()}
        if abs(ledger - chain) > self.cfg.mismatch_threshold_micro:
            report.treasury_mismatch = True
            self._alert("critical", "reconciliation_mismatch", {"scope": "treasury USDC", "ledger_micro": ledger,
                                                                "onchain_micro": chain, "diff_micro": chain - ledger},
                        dedup=f"treasury_mismatch:{date_key}")

    # (d) -------------------------------------------------------------------------------------------------------------
    def check_solvency(self, report: ReconcileReport, date_key: str) -> None:
        assert self.solvency is not None
        led = {k: int(v) for k, v in dict(self.solvency.solvency_ledger()).items()}
        chain = report.treasury_chain_micro
        if chain is None:
            chain = int(self.treasury.treasury_usdc_micro())
        assets = chain + max(0, led.get("builder_receivable", 0)) + max(0, led.get("stripe_clearing", 0))
        liabilities = sum(max(0, led.get(k, 0)) for k in (
            "user_fee_balances_positive", "creator_payables", "referrer_payables", "ps_pending", "withdrawals_pending",
            "payouts_pending", "refunds_pending", "suspense"))
        led.update({"treasury_chain": chain, "assets": assets, "liabilities": liabilities})
        report.solvency = led
        report.solvency_shortfall_micro = max(0, liabilities - assets)
        if liabilities - assets > self.cfg.mismatch_threshold_micro:
            self._alert("critical", "solvency_shortfall", {
                "assets_micro": assets, "liabilities_micro": liabilities, "shortfall_micro": liabilities - assets,
                "user_debt_micro": led.get("user_debt", 0)}, dedup=f"solvency:{date_key}")

    # (e) -------------------------------------------------------------------------------------------------------------
    def check_stripe_clearing(self, report: ReconcileReport, date_key: str) -> None:
        assert self.stripe is not None
        if report.stripe_clearing_status == "unsupported_currency":
            return                                     # already reported by the booking step
        ledger = int(self.ledger.balance(STRIPE_CLEARING))
        try:
            stripe = int(self.stripe.clearing_balance_micro())
        except Exception as exc:
            if self._unsupported_currency(report, exc, date_key):
                return
            raise
        report.stripe_clearing_ledger_micro, report.stripe_clearing_stripe_micro = ledger, stripe
        report.stripe_clearing_status = "ok"
        if abs(ledger - stripe) > self.cfg.mismatch_threshold_micro:
            report.stripe_clearing_status = "mismatch"
            self._alert("critical", "reconciliation_mismatch", {"scope": "stripe clearing", "ledger_micro": ledger,
                                                                "stripe_micro": stripe, "diff_micro": stripe - ledger},
                        dedup=f"stripe_clearing_mismatch:{date_key}")

    # (e) booking --------------------------------------------------------------------------------------------------
    def book_stripe_payouts(self, report: ReconcileReport, date_key: str) -> None:
        """Stripe payouts / payout reversals since the last run → ledger (idempotent on the balance transaction id)."""
        assert self.stripe is not None
        reader = getattr(self.stripe, "payout_movements", None)
        if reader is None or self.poster is None:
            return
        since = self._cursor("stripe_payouts")
        try:
            movements = list(reader(max(0, (since or 0) - CURSOR_OVERLAP_MS)))
        except Exception as exc:
            if self._unsupported_currency(report, exc, date_key):
                return
            raise
        newest = since or 0
        for m in movements:
            amount, fee = int(m.amount_micro), int(m.fee_micro)
            if amount <= 0 or fee < 0:
                raise ValueError("stripe payout movement with a non-positive amount")
            if m.reversal:
                if fee:
                    # a reversal carrying a fee has no exact rule-shaped posting: leave it to ops, never guess
                    self._alert("warn", "stripe_payout_unbooked", {"txn": m.txn_id, "payout": m.payout_id,
                                                                    "amount_micro": amount, "fee_micro": fee},
                                dedup=f"stripe_payout_unbooked:{m.txn_id}")
                    continue
                key, kind = f"stripe:payout_reversal:{m.txn_id}", "stripe_payout_reversal"
                lines = [LedgerLine(STRIPE_CLEARING, amount), LedgerLine(BANK_PAYOUTS, -amount)]
                memo = f"Stripe payout {m.payout_id} failed / cancelled: back in the Stripe balance"
            else:
                key, kind = f"stripe:payout:{m.txn_id}", "stripe_payout"
                lines = [LedgerLine(BANK_PAYOUTS, amount), LedgerLine(STRIPE_CLEARING, -(amount + fee))]
                if fee:
                    lines.append(LedgerLine(EXPENSE_STRIPE_FEES, fee))
                memo = f"Stripe payout {m.payout_id} to the bank"
            with self._atomic():
                _tx, created = self.poster.post_transaction(idempotency_key=key, kind=kind, memo=memo[:200],
                                                            lines=lines, created_by=CREATED_BY)
            if created:
                report.stripe_payouts_booked += 1
                report.stripe_payouts_booked_micro += -amount if m.reversal else amount
            newest = max(newest, int(m.created_ms))
        self._set_cursor("stripe_payouts", newest)

    def _unsupported_currency(self, report: ReconcileReport, exc: BaseException, date_key: str) -> bool:
        if getattr(exc, "code", "") != "stripe_currency_unsupported":
            return False
        report.stripe_clearing_status = "unsupported_currency"
        self._alert("warn", "stripe_clearing_unsupported_currency",
                    {"detail": getattr(exc, "message", str(exc))[:200], **dict(getattr(exc, "details", {}) or {})},
                    dedup=f"stripe_clearing_unsupported_currency:{date_key}")
        return True

    # (f) -------------------------------------------------------------------------------------------------------------
    def book_builder_claims(self, report: ReconcileReport, date_key: str) -> None:
        """Builder rewards claimed into the treasury since the last run → treasury:hl_usdc / builder:hl_receivable."""
        assert self.builder_claims is not None
        if not getattr(self.builder_claims, "claims_into_treasury", True):
            report.builder_claims_status = "outside_treasury"
            return
        if self.poster is None:
            return
        since = self._cursor("builder_claims")
        if since is None:
            since = int(getattr(self.builder_claims, "since_ms", 0) or 0)
        newest = since
        for c in self.builder_claims.builder_reward_claims(max(0, since - CURSOR_OVERLAP_MS)):
            amount = int(c.amount_micro)
            if amount <= 0:
                continue
            with self._atomic():
                _tx, created = self.poster.post_transaction(
                    idempotency_key=f"builder_claim:{c.ref}", kind="builder_rewards_claim",
                    memo=f"builder rewards claimed into the treasury {c.ref[:40]}",
                    lines=[LedgerLine(TREASURY_ACCOUNT, amount), LedgerLine(BUILDER_RECEIVABLE, -amount)],
                    created_by=CREATED_BY)
            if created:
                report.builder_claims_booked += 1
                report.builder_claims_booked_micro += amount
            newest = max(newest, int(c.time_ms))
        self._set_cursor("builder_claims", newest)
        report.builder_claims_status = "ok"

    def check_builder_receivable(self, report: ReconcileReport, date_key: str) -> None:
        """Ledger receivable + our builder fees not yet recognised (settlement lag) ≈ rewards claimable on-chain."""
        assert self.builder_claims is not None
        ledger = int(self.ledger.balance(BUILDER_RECEIVABLE))
        pending_fn = getattr(self.repo, "unrecognised_builder_fees_micro", None)
        pending = int(pending_fn()) if pending_fn is not None else 0
        chain = int(self.builder_claims.unclaimed_builder_rewards_micro())
        report.builder_receivable_ledger_micro = ledger
        report.builder_unrecognised_micro = pending
        report.builder_unclaimed_chain_micro = chain
        if abs(ledger + pending - chain) > self.cfg.mismatch_threshold_micro:
            report.builder_receivable_mismatch = True
            self._alert("critical", "reconciliation_mismatch", {
                "scope": "builder receivable", "ledger_micro": ledger, "unrecognised_micro": pending,
                "onchain_unclaimed_micro": chain, "diff_micro": chain - ledger - pending,
                "claims_status": report.builder_claims_status}, dedup=f"builder_receivable_mismatch:{date_key}")

    # helpers ---------------------------------------------------------------------------------------------------------
    def _atomic(self) -> Any:
        return self.uow.atomic() if self.uow is not None else nullcontext()

    def _cursor(self, name: str) -> int | None:
        get = getattr(self.repo, "get_cursor", None)
        return get(name) if get is not None else None

    def _set_cursor(self, name: str, value_ms: int) -> None:
        put = getattr(self.repo, "set_cursor", None)
        if put is not None and value_ms:
            put(name, int(value_ms))

    def _alert(self, severity: str, kind: str, payload: dict[str, Any], *, user_id: str | None = None,
               dedup: str | None = None) -> None:
        try:
            self.alerts.emit(AlertEvent(severity=severity, kind=kind, payload=payload, user_id=user_id,
                                        dedup_key=dedup))
        except Exception:
            log.error("alert_emit_failed", exc_info=True, extra={"fields": {"kind": kind}})
