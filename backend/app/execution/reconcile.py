"""Daily reconciliation (SPEC §5.5, §5.6; ``/internal/reconcile``).

(a) positions: last executed target per (subscription, coin) vs the on-chain position → drift alerts.
(b) builder fees: Σ builder fees of fills in our DB vs builder rewards accrued on-chain → critical alert if > $1.
(c) treasury: ledger balance of ``treasury:hl_usdc`` vs the treasury's on-chain USDC → critical alert if > $1.

Every check is isolated: one failing (e.g. an info endpoint outage) is reported and the others still run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.logging import get_logger
from app.money import BPS, usd

from .ports import (
    AlertEvent,
    AlertSink,
    BuilderRewardsReader,
    LedgerReader,
    PositionReader,
    ReconcileRepo,
    TreasuryReader,
)

log = get_logger("app.execution.reconcile")

TREASURY_ACCOUNT = "treasury:hl_usdc"


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
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "positions_checked": self.positions_checked,
            "drifts": [{"subscription_id": d.subscription_id, "coin": d.coin, "expected_micro": d.expected_micro,
                        "actual_micro": d.actual_micro} for d in self.drifts],
            "builder_db_micro": self.builder_db_micro, "builder_chain_micro": self.builder_chain_micro,
            "builder_mismatch": self.builder_mismatch,
            "treasury_ledger_micro": self.treasury_ledger_micro, "treasury_chain_micro": self.treasury_chain_micro,
            "treasury_mismatch": self.treasury_mismatch, "errors": list(self.errors),
        }


class Reconciler:
    def __init__(self, *, repo: ReconcileRepo, positions: PositionReader, builder_rewards: BuilderRewardsReader,
                 treasury: TreasuryReader, ledger: LedgerReader, alerts: AlertSink,
                 config: ReconcileConfig | None = None) -> None:
        self.repo = repo
        self.positions = positions
        self.builder_rewards = builder_rewards
        self.treasury = treasury
        self.ledger = ledger
        self.alerts = alerts
        self.cfg = config or ReconcileConfig()

    def run(self, date_key: str) -> ReconcileReport:
        """``date_key`` (e.g. "2026-09-30") scopes alert dedup keys to one run per day."""
        report = ReconcileReport()
        for name, fn in (("positions", self.check_positions), ("builder_fees", self.check_builder_fees),
                         ("treasury", self.check_treasury)):
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
                        user_id=e.user_id, dedup=f"drift:{e.subscription_id}:{e.coin}:{date_key}")

    # (b) -------------------------------------------------------------------------------------------------------------
    def check_builder_fees(self, report: ReconcileReport, date_key: str) -> None:
        db = int(self.repo.total_builder_fees_micro())
        chain = int(self.builder_rewards.cumulative_builder_rewards_micro())
        report.builder_db_micro, report.builder_chain_micro = db, chain
        if abs(db - chain) > self.cfg.mismatch_threshold_micro:
            report.builder_mismatch = True
            self._alert("critical", "builder_fee_mismatch", {"db_micro": db, "chain_micro": chain,
                                                             "diff_micro": chain - db},
                        dedup=f"builder_mismatch:{date_key}")

    # (c) -------------------------------------------------------------------------------------------------------------
    def check_treasury(self, report: ReconcileReport, date_key: str) -> None:
        ledger = int(self.ledger.balance(TREASURY_ACCOUNT))  # asset: debit-positive
        chain = int(self.treasury.treasury_usdc_micro())
        report.treasury_ledger_micro, report.treasury_chain_micro = ledger, chain
        if abs(ledger - chain) > self.cfg.mismatch_threshold_micro:
            report.treasury_mismatch = True
            self._alert("critical", "treasury_mismatch", {"ledger_micro": ledger, "chain_micro": chain,
                                                          "diff_micro": chain - ledger},
                        dedup=f"treasury_mismatch:{date_key}")

    def _alert(self, severity: str, kind: str, payload: dict[str, Any], *, user_id: str | None = None,
               dedup: str | None = None) -> None:
        try:
            self.alerts.emit(AlertEvent(severity=severity, kind=kind, payload=payload, user_id=user_id,
                                        dedup_key=dedup))
        except Exception:
            log.error("alert_emit_failed", exc_info=True, extra={"fields": {"kind": kind}})
