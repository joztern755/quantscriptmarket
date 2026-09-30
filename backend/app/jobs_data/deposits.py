"""Treasury deposit scan — ``app.hl.deposits.scan(db, now)`` (``/internal/deposits-scan``; SPEC §0, §8).

``userNonFundingLedgerUpdates(treasury)`` since the cursor (minus an overlap) → ``app.hl.deposits.detect_deposits``
(every sender; users resolved here from VERIFIED wallets) → ``app.payments.usdc.credit_from_detection`` → one ledger
transaction per transfer through ``app.ledger.service.post_transaction`` with idempotency key ``usdc_hl:{hash}``:

* credit  → debit ``treasury:hl_usdc`` / credit ``user:{id}:fee_balance`` (kind ``deposit``) + ``deposits`` row
  (method usdc_hl, external_ref = hash, credited, withdrawable). Identical to the API's POST /deposits/usdc/confirm
  path (same key, same entries, same deposits upsert), so whichever runs first wins and the other is a no-op.
* held    → debit ``treasury:hl_usdc`` / credit ``suspense:usdc_unattributed`` (kind ``deposit_held``) under the SAME
  key, so a transfer is booked exactly once: if its sender verifies the wallet later, the automatic credit is refused
  (Conflict) and ops releases it from suspense by hand (e.g. key ``suspense_release:{hash}``). Ops event
  ``topup_held`` (+ the user's ``topup_held`` event when the sender is a known user, e.g. below the minimum).
* bridge deposits (no sender) and odd transfers into the treasury → ops events only (not booked).

Every transfer is its own DB transaction; the cursor advances only past transfers that were booked, so a failure is
retried on the next call. A credit calls ``app.alerts.delivery.on_balance_changed`` in the same transaction. Events go to ``events_outbox`` (the alerts module delivers them).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.errors import AppError
from app.jobs_data import _db
from app.jobs_data.hl import WeightPacer, list_weight, make_info_client

__all__ = ["deposits_scan", "SUSPENSE_ACCOUNT", "LEDGER_PAGE"]

JOB = "deposits"
SUSPENSE_ACCOUNT = "suspense:usdc_unattributed"
TREASURY_ACCOUNT = "treasury:hl_usdc"
LEDGER_PAGE = 500
CREATED_BY = "system:deposits_scan"


@dataclass
class DepositsReport:
    fetched: int = 0
    detected: int = 0
    credited: int = 0
    credited_micro: int = 0
    already_booked: int = 0
    held: int = 0
    held_micro: int = 0
    ignored: int = 0
    unattributable: int = 0
    skipped: int = 0
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    skipped_reason: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["errors"] = self.errors[:20]
        return d


def _alert_to_event(conn: Any, alert: Any) -> None:
    sev = getattr(getattr(alert, "severity", None), "value", None) or str(getattr(alert, "severity", "info"))
    data = dict(getattr(alert, "data", {}) or {})
    key = getattr(alert, "key", None)
    user_id = getattr(alert, "user_id", None)
    if user_id is None and "method" in data:                  # ops text carries the sender: shorten it
        data["method"] = " ".join(_db.short_addr(w) if w.startswith("0x") and len(w) == 42 else w
                                  for w in str(data["method"]).split())
    _db.emit_event(conn, kind=str(alert.kind), payload=data, user_id=user_id, severity=sev,
                   dedup_key=(("ops:" if user_id is None else "") + key) if key else None)


def deposits_scan(db: Any, now: datetime, *, info: Any = None, settings: Any = None, max_pages: int = 10,
                  overlap_minutes: int = 60, initial_lookback_days: int = 14, max_seconds: float = 120.0,
                  weight_per_minute: int = 600, pacer: Optional[WeightPacer] = None) -> dict[str, Any]:
    from app.hl.deposits import detect_deposits
    from app.ledger import service as ledger
    from app.payments.usdc import credit_from_detection

    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    report = DepositsReport()
    treasury = (settings.treasury_address or "").lower()
    if not treasury:
        report.skipped_reason = "treasury address not configured"
        return report.as_dict()
    now_ms = _db.now_ms(now)
    info = make_info_client(settings, info=info)
    pacer = pacer or WeightPacer(weight_per_minute, max_seconds=max_seconds)
    with _db.transaction(db) as conn:
        cur, _state = _db.get_cursor(conn, JOB, treasury)
    start = (cur - overlap_minutes * 60_000) if cur is not None else now_ms - initial_lookback_days * 86_400_000

    updates: dict[tuple[Any, ...], dict] = {}
    cursor = max(0, start)
    try:
        for _ in range(max_pages):
            est = list_weight(LEDGER_PAGE)
            pacer.spend(est)
            report.requests += 1
            page = info.user_non_funding_ledger_updates(treasury, cursor, now_ms)
            pacer.settle(list_weight(len(page)), est)
            new, last = 0, cursor
            for u in page:
                t = u.get("time")
                if isinstance(t, int):
                    last = max(last, t)
                k = (u.get("hash"), t, _db.jdump(u.get("delta")))
                if k not in updates:
                    updates[k] = u
                    new += 1
            if len(page) < LEDGER_PAGE or new == 0:
                break
            cursor = last
        else:
            report.complete = False
    except AppError as e:
        report.errors.append(f"fetch:{type(e).__name__}")
        return report.as_dict()
    items = sorted(updates.values(), key=lambda u: int(u.get("time") or 0))
    report.fetched = len(items)
    scan = detect_deposits(items, treasury_address=treasury, verified_wallets=None, since_ms=None)
    report.skipped = len(scan.skipped)
    report.detected = len(scan.deposits) + len(scan.unverified)

    min_topup = settings.economics.min_topup_micro
    booked_until: Optional[int] = None
    failed_at: Optional[int] = None
    for det in sorted([*scan.deposits, *scan.unverified], key=lambda d: d.time):
        try:
            with _db.transaction(db) as conn:
                def lookup(addr: str, _conn: Any = conn) -> Optional[str]:
                    r = _db.one(_conn, """SELECT user_id::text AS user_id FROM wallets
                                           WHERE master_address = :a AND verified_at IS NOT NULL""", a=addr)
                    return str(r["user_id"]) if r else None

                outcome = credit_from_detection(det, treasury_address=treasury, min_topup_micro=min_topup,
                                                user_for_address=lookup)
                if outcome.credit is not None:
                    _book_credit(conn, ledger, outcome.credit, report)
                elif outcome.held is not None:
                    _book_held(conn, ledger, det, outcome.held, report)
                else:
                    report.ignored += 1
                for alert in outcome.alerts:
                    _alert_to_event(conn, alert)
            booked_until = det.time
        except Exception as e:  # noqa: BLE001 - stop here; the cursor stays before this transfer (retried)
            report.errors.append(f"{det.hash[:12]}:{type(e).__name__}")
            _db.log.error("deposit_booking_failed", exc_info=True, extra={"fields": {"hash": det.hash[:12]}})
            failed_at = det.time
            break

    with _db.transaction(db) as conn:
        for raw in scan.unattributable:
            report.unattributable += 1
            h = str(raw.get("hash") or "")
            _db.ops_alert(conn, "treasury_unattributable_deposit",
                          {"type": (raw.get("delta") or {}).get("type"), "usdc": (raw.get("delta") or {}).get("usdc"),
                           "time_ms": raw.get("time"), "hash": h[:18]},
                          severity="info", dedup_key=f"treasury_unattributable:{h}:{raw.get('time')}")
        for raw, reason in scan.skipped:
            if reason.startswith("self-transfer"):
                continue
            h = str(raw.get("hash") or "")
            _db.ops_alert(conn, "treasury_transfer_skipped",
                          {"reason": reason[:120], "time_ms": raw.get("time"), "hash": h[:18],
                           "type": (raw.get("delta") or {}).get("type")},
                          severity="warn", dedup_key=f"treasury_skipped:{h}:{raw.get('time')}")
        if failed_at is not None:
            new_cur = max(0, failed_at - 1)
        else:
            newest = max((int(u.get("time") or 0) for u in items), default=None)
            new_cur = newest if newest is not None else now_ms - overlap_minutes * 60_000
            if report.complete:
                new_cur = max(new_cur, now_ms - overlap_minutes * 60_000)
        _db.set_cursor(conn, JOB, treasury, new_cur, {"last_run_ms": now_ms, "booked_until_ms": booked_until,
                                                      "complete": report.complete})
    _db.log.info("deposits_scan_done", extra={"fields": report.as_dict()})
    return report.as_dict()


def _book_credit(conn: Any, ledger: Any, instr: Any, report: DepositsReport) -> None:
    from app.api.store import SqlStore                 # the same deposits upsert as the API's confirm path

    existing = ledger.get_transaction(conn, instr.idempotency_key)
    if existing is not None and existing.kind != instr.kind:
        # booked to suspense earlier (e.g. the wallet was verified after the transfer): never book twice
        report.already_booked += 1
        _db.ops_alert(conn, "topup_held_now_attributable", {
            "hash": instr.external_ref[:18], "amount_micro": instr.amount_micro, "user_id": instr.user_id,
            "booked_as": existing.kind}, severity="warn", dedup_key=f"topup_held_now_attributable:{instr.external_ref}")
        return
    fee_account = f"user:{instr.user_id}:fee_balance"
    prev = -ledger.get_balance(conn, fee_account)
    tx = ledger.post_transaction(conn, instr.idempotency_key, instr.kind, instr.memo or "deposit",
                                 [(instr.debit_account, instr.amount_micro), (instr.credit_account, -instr.amount_micro)],
                                 CREATED_BY)
    if tx.created and fee_account in (instr.debit_account, instr.credit_account):
        _balance_changed(conn, ledger, instr.user_id, prev)
    meta = {k: (v if isinstance(v, (str, int, bool)) else str(v)) for k, v in dict(instr.meta or {}).items()
            if v is not None}
    meta["from"] = _db.short_addr(str(meta.get("from", "")))
    SqlStore().mark_deposit_credited(conn, user_id=instr.user_id, method=instr.method, external_ref=instr.external_ref,
                                     amount_micro=instr.amount_micro, tx_id=str(tx.id),
                                     withdrawable=bool(instr.withdrawable), meta=meta)
    if tx.created:
        report.credited += 1
        report.credited_micro += instr.amount_micro
    else:
        report.already_booked += 1


def _balance_changed(conn: Any, ledger: Any, user_id: str, prev: int) -> None:
    """Same-transaction low-balance hook (app.alerts.delivery.on_balance_changed) after a fee-balance posting made
    outside app.api.ledger_ops. A credit never crosses a threshold downwards, but every posting goes through the
    hook so the rule stays in one place. SAVEPOINT: a failed alert never undoes the credit."""
    from app.alerts import _db as alerts_db
    from app.alerts.delivery import on_balance_changed

    try:
        new = -ledger.get_balance(conn, f"user:{user_id}:fee_balance")
        with alerts_db.savepoint(conn):
            on_balance_changed(conn, user_id, prev, new, raise_errors=True)
    except Exception as e:  # noqa: BLE001 - alerts must never block a deposit credit
        _db.log.warning("deposit_balance_hook_failed", extra={"fields": {"user_id": user_id,
                                                                         "error": type(e).__name__}})


def _book_held(conn: Any, ledger: Any, det: Any, reason: str, report: DepositsReport) -> None:
    amount = int(det.amount_micro)
    if amount <= 0:
        report.ignored += 1
        return
    existing = ledger.get_transaction(conn, f"usdc_hl:{det.hash}")
    if existing is not None and existing.kind != "deposit_held":
        report.already_booked += 1                    # already credited to a user (e.g. via the API confirm path)
        return
    ledger.ensure_account(conn, SUSPENSE_ACCOUNT, "liability", None, non_negative=False)
    tx = ledger.post_transaction(conn, f"usdc_hl:{det.hash}", "deposit_held",
                                 f"USDC held for review {det.hash[:10]}… ({reason[:60]})",
                                 [(TREASURY_ACCOUNT, amount), (SUSPENSE_ACCOUNT, -amount)], CREATED_BY)
    if tx.created:
        report.held += 1
        report.held_micro += amount
    else:
        report.already_booked += 1
