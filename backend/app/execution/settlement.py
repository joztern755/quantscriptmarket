"""Daily settlement job (SPEC §1, §1.1; ``/internal/settle-daily``, 00:30 UTC).

For ``settle_date`` D (PnL cut-off = D 00:00 UTC):

1. Profit share, per subscription: attributed PnL in (cursor, D 00:00] → ``domain.profit_share.settle`` →
   one balanced ledger transaction ``ps:{subscription_id}:{D}`` (user fee balance debited, creator payable and
   platform profit-share revenue credited; in-house → all platform). The ledger post and the subscription's
   cum_pnl / hwm / cursor update commit in one DB transaction (``UnitOfWork``). Re-running is a no-op
   (settlement row + ledger idempotency key + cursor).
2. Subscription renewals (monthly price prepaid, 97% creator / 3% platform) with idempotency key
   ``sub:{subscription_id}:{period_end date}``, and the fee-balance status machine (``billing.next_status``):
   active → past_due → reduce_only after the grace period; back to active once the balance covers what is due.
3. Platform plan renewals (``plan:{user_id}:{period_end date}``); unpaid → past_due, after grace → downgraded
   to free.
4. Builder-fee revenue recognition per fill (``bf:{trading_address}:{tid}`` — a tid identifies the TRADE and is
   shared by both counterparties, so the fill key is (trading_address, tid) like the fills UNIQUE constraint):
   builder receivable debited, creator / referrer / platform credited per ``domain.fees.split_builder_fee`` with
   the referrer's current tier.

Ledger sign convention (SPEC §4): + debit / − credit, Σ per transaction = 0. The user fee balance is a
liability, so the user's available balance is ``−balance(user:{id}:fee_balance)``.

User events (``UserEventSink``, same DB transaction as the posting they describe — SPEC §12 alert kinds):
- every posting on ``user:{id}:fee_balance`` (profit share, subscription renewal, plan renewal) calls
  ``fee_balance_changed(prev, new)`` → low-balance alerts (50 / 20 / 0 % of the monthly need, once per crossing);
- ``profit_share_charged`` per charge (amount, profit above the HWM, rate, strategy, settled period), dedup
  ``profit_share_charged:{ledger key}``; ``subscription_renewed`` per paid renewal, dedup ``subscription_renewed:{key}``.

Data-completeness guard (SPEC §1.1; RUNBOOK §13.4): a (subscription, day) is settled only when BOTH data jobs have
completely synced the subscription's trading address past the PnL cut-off + ``coverage_margin`` — ``fills-ingest``
(fills → realized PnL) and ``funding-scan`` (funding → attributed PnL), read through ``repo.data_coverage``
(``job_cursors``: a complete run counts up to its run time, an incomplete one up to its monotonic cursor). Otherwise
the subscription is DEFERRED: no ledger post, no cursor move, no renewal/status change on top (retried by the next
run — the ``settle-daily-retry`` Scheduler slots at 02:30 and 06:30 UTC, then the next day's run, which settles the
missed day together with the new one because PnL is summed from the subscription's own ``pnl_cursor``). One ops warn
event ``settlement_deferred`` per settle date (dedup ``settlement_deferred:{date}``). Needed coverage is
``min(cut-off, cancelled_at)`` (nothing is attributed to a cancelled subscription after it ended); a subscription
created after that point has nothing to settle and is never deferred. A subscription without a trading address, or
an address never synced, is deferred (fail closed).

Policy notes:
- Profit share is charged in full even if it drives the fee balance negative (it is owed on profit already
  realised in the user's own account); the subscription then moves to past_due. Renewals are prepaid: they
  are only charged when the balance covers them, and they are retried daily until paid.
- A renewal after a lapse starts a new period from the anchor strictly after ``now`` (no back-charging for months
  spent past_due / reduce_only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.logging import get_logger

from .ports import (
    AlertEvent,
    AlertSink,
    BillingPolicy,
    BuilderFeeFill,
    Clock,
    DataCoverage,
    FeeSplitter,
    LedgerLine,
    LedgerPoster,
    PlanAccount,
    ProfitShareCalculator,
    ReferralLookup,
    SettlementRepo,
    SettlementSubscription,
    UnitOfWork,
    UserEventSink,
)

log = get_logger("app.execution.settlement")

BILLABLE = ("active", "past_due", "reduce_only")

ACC_PLATFORM_PROFIT_SHARE = "platform:revenue:profit_share"
ACC_PLATFORM_SUBSCRIPTION = "platform:revenue:subscription"
ACC_PLATFORM_PLANS = "platform:revenue:plans"
ACC_PLATFORM_BUILDER = "platform:revenue:builder"
ACC_BUILDER_RECEIVABLE = "builder:hl_receivable"


def fee_balance_account(user_id: str) -> str:
    return f"user:{user_id}:fee_balance"


def creator_payable_account(user_id: str) -> str:
    return f"creator:{user_id}:payable"


def referrer_payable_account(user_id: str) -> str:
    return f"referrer:{user_id}:payable"


def profit_share_key(subscription_id: str, settle_date: date) -> str:
    return f"ps:{subscription_id}:{settle_date.isoformat()}"


def renewal_key(subscription_id: str, period_end: datetime) -> str:
    return f"sub:{subscription_id}:{period_end.astimezone(timezone.utc).date().isoformat()}"


def plan_key(user_id: str, period_end: datetime) -> str:
    return f"plan:{user_id}:{period_end.astimezone(timezone.utc).date().isoformat()}"


def builder_fee_key(trading_address: str, tid: str) -> str:
    if not trading_address or not str(tid):
        raise ValueError("builder fee key needs trading_address and tid")
    return f"bf:{trading_address.lower()}:{tid}"


def _lines(*pairs: tuple[str, int]) -> list[LedgerLine]:
    """Drop zero lines; assert the transaction balances (never post an unbalanced transaction)."""
    out = [LedgerLine(code, int(amt)) for code, amt in pairs if amt]
    if sum(ln.amount_micro for ln in out) != 0:
        raise AssertionError("unbalanced ledger transaction")
    return out


@dataclass
class SettlementReport:
    settle_date: str
    subscriptions_seen: int = 0
    profit_share_settled: int = 0
    profit_share_skipped: int = 0
    profit_share_deferred: int = 0
    deferred: list[dict[str, Any]] = field(default_factory=list)   # [{subscription_id, missing, needed_until}]
    profit_share_charged_micro: int = 0
    renewals_charged: int = 0
    renewals_charged_micro: int = 0
    renewals_failed: int = 0
    status_changes: list[tuple[str, str, str]] = field(default_factory=list)   # (subscription_id, from, to)
    plans_renewed: int = 0
    plans_past_due: int = 0
    plans_downgraded: int = 0
    builder_fills_recognised: int = 0
    builder_fees_recognised_micro: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Settlement:
    def __init__(self, *, repo: SettlementRepo, ledger: LedgerPoster, uow: UnitOfWork,
                 profit_share: ProfitShareCalculator, fees: FeeSplitter, billing: BillingPolicy,
                 referrals: ReferralLookup, alerts: AlertSink, clock: Clock, builder_page_size: int = 1000,
                 created_by: str = "system:settlement", events: UserEventSink | None = None,
                 require_data_coverage: bool = True, coverage_margin: timedelta = timedelta(minutes=2)) -> None:
        self.repo = repo
        self.ledger = ledger
        self.uow = uow
        self.ps = profit_share
        self.fees = fees
        self.billing = billing
        self.referrals = referrals
        self.alerts = alerts
        self.clock = clock
        self.page = builder_page_size
        self.created_by = created_by
        self.events = events
        self.require_data_coverage = require_data_coverage
        self.coverage_margin = coverage_margin

    # --------------------------------------------------------------------------------------------------------- entry

    def settle_daily(self, settle_date: date | None = None, now: datetime | None = None) -> SettlementReport:
        now = now or self.clock.now()
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware UTC")
        settle_date = settle_date or now.astimezone(timezone.utc).date()
        cutoff = datetime.combine(settle_date, time(0, 0), tzinfo=timezone.utc)
        if cutoff > now:
            raise ValueError("cannot settle a day that has not started")
        report = SettlementReport(settle_date=settle_date.isoformat())

        subs = list(self.repo.subscriptions_to_settle())
        coverage = self._coverage(subs, report)
        for sub in subs:
            report.subscriptions_seen += 1
            missing = self._missing_data(sub, settle_date, cutoff, coverage)
            if missing is not None:
                report.profit_share_deferred += 1
                report.deferred.append(missing)
                log.warning("settlement_deferred", extra={"fields": missing})
                continue  # no ledger post, no renewal / status change on top; retried by the next run
            try:
                self._settle_profit_share(sub, settle_date, cutoff, report)
            except Exception as exc:
                self._fail(report, f"profit_share:{sub.id}", exc, user_id=sub.user_id)
                continue  # do not renew on top of a failed settlement; retried on the next run
            try:
                self._renew_and_update_status(sub, now, report)
            except Exception as exc:
                self._fail(report, f"renewal:{sub.id}", exc, user_id=sub.user_id)

        for acct in self.repo.plans_due(now):
            try:
                self._renew_plan(acct, now, report)
            except Exception as exc:
                self._fail(report, f"plan:{acct.user_id}", exc, user_id=acct.user_id)

        try:
            self.recognise_builder_fees(cutoff, report)
        except Exception as exc:
            self._fail(report, "builder_fees", exc)

        if report.deferred:
            self._ops_event("warn", "settlement_deferred", {
                "settle_date": settle_date.isoformat(), "cutoff": cutoff.isoformat(),
                "deferred": len(report.deferred), "subscriptions": [d["subscription_id"] for d in report.deferred[:20]],
                "missing": sorted({m for d in report.deferred for m in d["missing"]})},
                dedup=f"settlement_deferred:{settle_date.isoformat()}")

        log.info("settlement_done", extra={"fields": report.as_dict()})
        return report

    # ------------------------------------------------------------------------------------------- data-completeness guard

    def _coverage(self, subs: list[SettlementSubscription], report: SettlementReport) -> dict[str, DataCoverage]:
        if not self.require_data_coverage:
            return {}
        addrs = sorted({s.trading_address.lower() for s in subs if s.trading_address})
        if not addrs:
            return {}
        try:
            return {str(k).lower(): v for k, v in dict(self.repo.data_coverage(addrs)).items()}
        except Exception as exc:   # unknown coverage → every subscription defers (fail closed)
            self._fail(report, "data_coverage", exc)
            return {}

    def _missing_data(self, sub: SettlementSubscription, settle_date: date, cutoff: datetime,
                      coverage: dict[str, DataCoverage]) -> dict[str, Any] | None:
        """None when the subscription may be settled for this cut-off, else a description of what is missing."""
        if not self.require_data_coverage:
            return None
        if self.repo.is_settled(sub.id, settle_date) or (sub.pnl_cursor is not None and sub.pnl_cursor >= cutoff):
            return None                                      # already settled: nothing will be posted anyway
        needed = cutoff
        if sub.cancelled_at is not None and sub.cancelled_at < needed:
            needed = sub.cancelled_at
        if sub.pnl_cursor is not None and sub.pnl_cursor >= needed:
            return None                                      # nothing attributable after the cursor
        if sub.created_at is not None and sub.created_at >= needed:
            return None                                      # started after the needed point: nothing to settle
        need_ms = int((needed + self.coverage_margin).timestamp() * 1000)
        cov = coverage.get((sub.trading_address or "").lower()) if sub.trading_address else None
        missing = []
        if cov is None or cov.fills_ms is None or cov.fills_ms < need_ms:
            missing.append("fills_ingest")
        if cov is None or cov.funding_ms is None or cov.funding_ms < need_ms:
            missing.append("funding_scan")
        if not missing:
            return None
        return {"subscription_id": sub.id, "missing": missing, "needed_until": needed.isoformat(),
                "fills_ms": cov.fills_ms if cov else None, "funding_ms": cov.funding_ms if cov else None}

    # ------------------------------------------------------------------------------------------------- profit share

    def _settle_profit_share(self, sub: SettlementSubscription, settle_date: date, cutoff: datetime,
                             report: SettlementReport) -> None:
        key = profit_share_key(sub.id, settle_date)
        if self.repo.is_settled(sub.id, settle_date) or (sub.pnl_cursor is not None and sub.pnl_cursor >= cutoff):
            report.profit_share_skipped += 1
            return
        if not sub.in_house and not sub.creator_user_id:
            raise ValueError("third-party strategy without creator")
        delta = self.repo.pnl_since(sub.id, sub.pnl_cursor, cutoff)
        charge = self.ps.settle(cum_pnl_micro=sub.cum_pnl_micro, hwm_micro=sub.hwm_micro,
                                pnl_delta_micro=delta.total_micro, creator_bps=sub.profit_share_bps,
                                in_house=sub.in_house)
        if charge.total_micro < 0 or charge.creator_micro < 0 or charge.platform_micro < 0:
            raise AssertionError("negative profit-share charge")

        with self.uow.atomic():
            tx_id = None
            if charge.total_micro > 0:
                creator_acct = creator_payable_account(sub.creator_user_id) if sub.creator_user_id and not sub.in_house else None
                creator_amt = charge.creator_micro if creator_acct else 0
                platform_amt = charge.platform_micro + (charge.creator_micro - creator_amt)
                pairs = [(fee_balance_account(sub.user_id), charge.total_micro),
                         (ACC_PLATFORM_PROFIT_SHARE, -platform_amt)]
                if creator_acct:
                    pairs.append((creator_acct, -creator_amt))
                prev = self._spendable(sub.user_id)
                tx_id, created = self.ledger.post_transaction(
                    idempotency_key=key, kind="profit_share",
                    memo=f"profit share {settle_date.isoformat()} profit_micro={charge.profit_micro}",
                    lines=_lines(*pairs), created_by=self.created_by)
                if created:
                    self._balance_changed(sub.user_id, prev)
                    self._event(sub.user_id, "profit_share_charged", "info", {
                        "amount_micro": charge.total_micro, "profit_micro": charge.profit_micro,
                        "rate_bps": self._profit_share_rate_bps(sub),
                        "creator_micro": creator_amt, "platform_micro": platform_amt,
                        "strategy_id": sub.strategy_id, "subscription_id": sub.id,
                        "period_start": sub.pnl_cursor.isoformat() if sub.pnl_cursor else sub.created_at.isoformat(),
                        "period_end": cutoff.isoformat(), "settle_date": settle_date.isoformat()},
                        dedup=f"profit_share_charged:{key}")
            self.repo.save_profit_share(sub.id, settle_date, cum_pnl_micro=charge.new_cum_pnl_micro,
                                        hwm_micro=charge.new_hwm_micro, pnl_cursor=cutoff, ledger_tx_id=tx_id)
        report.profit_share_settled += 1
        report.profit_share_charged_micro += charge.total_micro
        log.info("profit_share_settled", extra={"fields": {
            "subscription_id": sub.id, "settle_date": settle_date.isoformat(), "pnl_delta_micro": delta.total_micro,
            "profit_micro": charge.profit_micro, "charge_micro": charge.total_micro}})

    # -------------------------------------------------------------------------------------- renewal + status machine

    def _renew_and_update_status(self, sub: SettlementSubscription, now: datetime, report: SettlementReport) -> None:
        if sub.status not in BILLABLE:
            return
        available = -self.ledger.balance(fee_balance_account(sub.user_id))
        renewal_due = sub.current_period_end is not None and sub.current_period_end <= now
        due = sub.price_monthly_micro if renewal_due else 0
        status, since = self.billing.next_status(status=sub.status, balance_micro=available, amount_due_micro=due,
                                                 past_due_since=sub.past_due_since, now=now)
        if renewal_due and status == "active":
            assert sub.current_period_end is not None
            key = renewal_key(sub.id, sub.current_period_end)
            new_end = self.billing.next_period_end(sub.created_at, max(now, sub.current_period_end))
            with self.uow.atomic():
                if due > 0:
                    creator_amt, platform_amt = self.fees.split_subscription(due)
                    if creator_amt + platform_amt != due:
                        raise AssertionError("subscription split does not sum to price")
                    if sub.in_house or not sub.creator_user_id:
                        platform_amt, creator_amt = platform_amt + creator_amt, 0
                    pairs = [(fee_balance_account(sub.user_id), due), (ACC_PLATFORM_SUBSCRIPTION, -platform_amt)]
                    if creator_amt:
                        pairs.append((creator_payable_account(sub.creator_user_id), -creator_amt))  # type: ignore[arg-type]
                    prev = self._spendable(sub.user_id)
                    _tx, created = self.ledger.post_transaction(
                        idempotency_key=key, kind="subscription_renewal",
                        memo=f"subscription renewal period_end={sub.current_period_end.isoformat()}",
                        lines=_lines(*pairs), created_by=self.created_by)
                    if created:
                        self._balance_changed(sub.user_id, prev)
                        self._event(sub.user_id, "subscription_renewed", "info", {
                            "amount_micro": due, "strategy_id": sub.strategy_id, "subscription_id": sub.id,
                            "period_end": new_end.isoformat()}, dedup=f"subscription_renewed:{key}")
                self.repo.set_period_end(sub.id, new_end)
                if status != sub.status or since != sub.past_due_since:
                    self.repo.set_status(sub.id, status, since)
            report.renewals_charged += 1
            report.renewals_charged_micro += due
        else:
            if renewal_due:
                report.renewals_failed += 1
            if status != sub.status or since != sub.past_due_since:
                self.repo.set_status(sub.id, status, since)
        if status != sub.status:
            report.status_changes.append((sub.id, sub.status, status))
            sev = "info" if status == "active" else "warn"
            self._alert(sev, f"subscription_{status}", {"subscription": sub.id, "strategy": sub.strategy_id,
                                                       "from": sub.status, "available_micro": available,
                                                       "due_micro": due},
                        user_id=sub.user_id, dedup=f"sub_status:{sub.id}:{status}:{now.date().isoformat()}")

    # ------------------------------------------------------------------------------------------------------ plans

    def _renew_plan(self, acct: PlanAccount, now: datetime, report: SettlementReport) -> None:
        if acct.plan == "free" or acct.plan_period_end is None or acct.plan_period_end > now:
            return
        available = -self.ledger.balance(fee_balance_account(acct.user_id))
        price = acct.price_monthly_micro
        if available >= price:
            new_end = self.billing.next_period_end(acct.anchor or acct.plan_period_end, max(now, acct.plan_period_end))
            with self.uow.atomic():
                if price > 0:
                    prev = self._spendable(acct.user_id)
                    _tx, created = self.ledger.post_transaction(
                        idempotency_key=plan_key(acct.user_id, acct.plan_period_end), kind="plan_renewal",
                        memo=f"plan {acct.plan} renewal period_end={acct.plan_period_end.isoformat()}",
                        lines=_lines((fee_balance_account(acct.user_id), price), (ACC_PLATFORM_PLANS, -price)),
                        created_by=self.created_by)
                    if created:
                        self._balance_changed(acct.user_id, prev)
                self.repo.set_plan_period(acct.user_id, new_end, None)
            report.plans_renewed += 1
            return
        if acct.past_due_since is None:
            self.repo.set_plan_period(acct.user_id, acct.plan_period_end, now)
            report.plans_past_due += 1
            self._alert("warn", "plan_past_due", {"plan": acct.plan, "available_micro": available, "due_micro": price},
                        user_id=acct.user_id, dedup=f"plan_past_due:{acct.user_id}:{acct.plan_period_end.date().isoformat()}")
        elif now - acct.past_due_since >= timedelta(hours=self.billing.grace_hours):
            with self.uow.atomic():
                self.repo.downgrade_plan(acct.user_id, "free")
                self.repo.set_plan_period(acct.user_id, None, None)
            report.plans_downgraded += 1
            self._alert("warn", "plan_downgraded", {"from": acct.plan, "to": "free"}, user_id=acct.user_id,
                        dedup=f"plan_downgraded:{acct.user_id}:{acct.plan_period_end.date().isoformat()}")

    # --------------------------------------------------------------------------------------------- builder revenue

    def recognise_builder_fees(self, until: datetime, report: SettlementReport) -> None:
        while True:
            fills = list(self.repo.unrecognised_builder_fee_fills(until, self.page))
            if not fills:
                return
            progressed = 0
            for f in fills:
                try:
                    self._recognise_fill(f, report)
                    progressed += 1
                except Exception as exc:
                    self._fail(report, f"builder_fee:{f.trading_address}:{f.tid}", exc)
            if progressed == 0 or len(fills) < self.page:
                return

    def _recognise_fill(self, f: BuilderFeeFill, report: SettlementReport) -> None:
        fee = int(f.builder_fee_micro)
        if fee < 0:
            raise ValueError("negative builder fee")
        attributed = f.subscription_id is not None
        if not attributed and fee > 0:   # our builder fee on a fill we cannot attribute (crash window) → ops look
            self._alert("warn", "builder_fee_unattributed_fill", {"tid": f.tid, "fee_micro": fee, "address": f.trading_address},
                        dedup=f"bf_unattributed:{f.trading_address}:{f.tid}")
        has_creator = attributed and not f.in_house and bool(f.creator_user_id)
        ref = self.referrals.referrer_share(f.user_id) if (attributed and f.user_id) else None
        split = self.fees.split_builder_fee(fee, in_house=not has_creator,
                                            referrer_share_bps=ref[1] if ref else None)
        if split.total_micro != fee:
            raise AssertionError("builder fee split does not sum to fee")
        pairs = [(ACC_BUILDER_RECEIVABLE, fee), (ACC_PLATFORM_BUILDER, -split.platform_micro)]
        if split.creator_micro:
            pairs.append((creator_payable_account(f.creator_user_id), -split.creator_micro))  # type: ignore[arg-type]
        if split.referrer_micro:
            assert ref is not None
            pairs.append((referrer_payable_account(ref[0]), -split.referrer_micro))
        with self.uow.atomic():
            tx_id = None
            if fee > 0:
                tx_id, _ = self.ledger.post_transaction(
                    idempotency_key=builder_fee_key(f.trading_address, f.tid), kind="builder_fee",
                    memo=f"builder fee fill tid={f.tid}", lines=_lines(*pairs), created_by=self.created_by)
            self.repo.mark_builder_fee_recognised(f.trading_address, f.tid, tx_id or "")
        report.builder_fills_recognised += 1
        report.builder_fees_recognised_micro += fee

    # ---------------------------------------------------------------------------------------------------- helpers

    def _spendable(self, user_id: str) -> int:
        return -self.ledger.balance(fee_balance_account(user_id))

    def _balance_changed(self, user_id: str, prev: int) -> None:
        """Low-balance alerts for a fee-balance posting (same transaction). Never fails the money movement."""
        if self.events is None:
            return
        try:
            self.events.fee_balance_changed(user_id=user_id, prev_micro=prev, new_micro=self._spendable(user_id),
                                            now=self.clock.now())
        except Exception:
            log.error("balance_hook_failed", exc_info=True, extra={"fields": {"user_id": user_id}})

    def _event(self, user_id: str, kind: str, severity: str, payload: dict[str, Any], *, dedup: str) -> None:
        if self.events is None:
            return
        try:
            self.events.emit(user_id=user_id, kind=kind, severity=severity, payload=payload, dedup_key=dedup)
        except Exception:
            log.error("user_event_failed", exc_info=True, extra={"fields": {"kind": kind}})

    def _profit_share_rate_bps(self, sub: SettlementSubscription) -> int:
        """Total rate the user pays (domain.profit_share: on_top → creator % + platform %; carved_out → creator %)."""
        econ = getattr(self.ps, "economics", None)
        platform = int(getattr(econ, "platform_profit_share_bps", 0) or 0)
        on_top = getattr(econ, "platform_profit_share_mode", "on_top") == "on_top"
        creator = int(sub.profit_share_bps)
        return creator + platform if on_top else creator

    def _ops_event(self, severity: str, kind: str, payload: dict[str, Any], *, dedup: str) -> None:
        """Ops event (user_id NULL) through the events outbox when available — its dedup key is permanent, so the
        retry runs of the same date add nothing — else through the alert sink."""
        if self.events is not None:
            try:
                self.events.emit(user_id=None, kind=kind, severity=severity, payload=payload,  # type: ignore[arg-type]
                                 dedup_key=dedup)
                return
            except Exception:
                log.error("ops_event_failed", exc_info=True, extra={"fields": {"kind": kind}})
        self._alert(severity, kind, payload, dedup=dedup)

    def _fail(self, report: SettlementReport, what: str, exc: Exception, *, user_id: str | None = None) -> None:
        report.errors.append(f"{what}:{type(exc).__name__}")
        log.error("settlement_step_failed", exc_info=True, extra={"fields": {"step": what, "error": type(exc).__name__}})
        self._alert("critical" if isinstance(exc, AssertionError) else "warn", "settlement_error",
                    {"step": what, "error": type(exc).__name__}, dedup=f"settlement_error:{what}:{report.settle_date}")

    def _alert(self, severity: str, kind: str, payload: dict[str, Any], *, user_id: str | None = None,
               dedup: str | None = None) -> None:
        try:
            self.alerts.emit(AlertEvent(severity=severity, kind=kind, payload=payload, user_id=user_id,
                                        dedup_key=dedup))
        except Exception:
            log.error("alert_emit_failed", exc_info=True, extra={"fields": {"kind": kind}})
