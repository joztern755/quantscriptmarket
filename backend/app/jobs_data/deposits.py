"""Treasury deposit scan — ``app.hl.deposits.scan(db, now)`` (``/internal/deposits-scan``; SPEC §0, §8).

``userNonFundingLedgerUpdates(treasury)`` since the cursor (minus an overlap) → ``app.hl.deposits.detect_deposits``
(every sender; users resolved here from VERIFIED wallets) → ``app.payments.usdc.credit_from_detection`` → one ledger
transaction per transfer through ``app.ledger.service.post_transaction`` with idempotency key ``usdc_hl:{hash}``:

* credit  → debit ``treasury:hl_usdc`` / credit ``user:{id}:fee_balance`` (kind ``deposit``) + ``deposits`` row
  (method usdc_hl, external_ref = hash, credited, withdrawable). Identical to the API's POST /deposits/usdc/confirm
  path (same key, same entries, same deposits upsert), so whichever runs first wins and the other is a no-op.
* held    → debit ``treasury:hl_usdc`` / credit ``suspense:usdc_unattributed`` (kind ``deposit_held``) under the SAME
  key, so a transfer is booked exactly once: if its sender verifies the wallet later, it is never credited
  automatically (it predates the verification, and the key is taken) — a re-scan raises the ops event
  ``topup_held_now_attributable`` once (only for transfers held because the sender was unknown) and ops release it from
  suspense by maker-checker (key ``suspense_release:{hash}``, RUNBOOK §13.3). Ops event ``topup_held`` (+ the user's
  ``topup_held`` event when the sender is a known user, e.g. below the minimum).
* bridge deposits (no sender) and odd transfers into the treasury → ops events only (not booked).

Scan requests first (``deposit_scan_requests``, upserted by POST /deposits/usdc/confirm — REVIEW_AUTH_API F1): before
the treasury window, every run serves the oldest pending requests (``served_at IS NULL``; at most
``max_scan_requests`` users, ``MAX_WALLETS_PER_REQUEST`` verified wallets each) by reading each requested wallet's OWN
``userNonFundingLedgerUpdates`` from the request's clamped ``since`` to now — a small window, so a user's transfer is
credited on the next run even when the treasury cursor is behind (backlog, incomplete run). Those transfers go
through exactly the same detection / credit / hold path (same ``usdc_hl:{hash}`` key; a hash seen in both windows is
booked once). Every request read is charged to the shared Hyperliquid budget through the job's pacer; when the budget
(or the run's deadline) has no room the remaining requests stay pending for the next run. A request is marked served
(``served_at``) only after all of its transfers were booked, and only if it was not re-requested meanwhile
(``requested_at`` unchanged). Requested-wallet transfers never move the treasury cursor forward.

Every transfer is its own DB transaction; the cursor advances only past transfers that were booked, so a failure is
retried on the next call. A credit calls ``app.alerts.delivery.on_balance_changed`` in the same transaction. Events go to ``events_outbox`` (the alerts module delivers them).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.errors import AppError
from app.jobs_data import _db
from app.jobs_data.hl import WeightPacer, list_weight, make_info_client, make_pacer

__all__ = ["deposits_scan", "SUSPENSE_ACCOUNT", "LEDGER_PAGE"]

JOB = "deposits"
SUSPENSE_ACCOUNT = "suspense:usdc_unattributed"
TREASURY_ACCOUNT = "treasury:hl_usdc"
LEDGER_PAGE = 500
CREATED_BY = "system:deposits_scan"
MAX_WALLETS_PER_REQUEST = 5
REQUEST_MAX_PAGES = 3


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
    scan_requests_served: int = 0
    scan_requests_deferred: int = 0
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
                  weight_per_minute: int = 600, pacer: Optional[WeightPacer] = None, rate_budget: Any = None,
                  max_scan_requests: int = 20) -> dict[str, Any]:
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
    pacer = pacer or make_pacer(db, settings, info=info, weight_per_minute=weight_per_minute,
                                max_seconds=max_seconds, rate_budget=rate_budget)
    info = make_info_client(settings, info=info)
    with _db.transaction(db) as conn:
        cur, _state = _db.get_cursor(conn, JOB, treasury)
    start = (cur - overlap_minutes * 60_000) if cur is not None else now_ms - initial_lookback_days * 86_400_000

    # 1) priority: the wallets of users who asked (POST /deposits/usdc/confirm) — small per-wallet windows
    requested, req_updates = _serve_requests(db, info, pacer, treasury, now_ms, max_scan_requests, report)

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
        if not req_updates:
            return report.as_dict()
        treasury_ok = False                   # still book what the requested wallets showed; cursor untouched
    else:
        treasury_ok = True
    items = sorted(updates.values(), key=lambda u: int(u.get("time") or 0))
    treasury_newest = max((int(u.get("time") or 0) for u in items), default=None)
    seen_hashes = {str(u.get("hash") or "").lower() for u in items}
    for u in req_updates:                     # a transfer seen in both windows is booked once (treasury copy wins)
        h = str(u.get("hash") or "").lower()
        if h and h in seen_hashes:
            continue
        seen_hashes.add(h)
        items.append(u)
    items.sort(key=lambda u: int(u.get("time") or 0))
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

                def verified_at(addr: str, _conn: Any = conn) -> Any:
                    # REVIEW_AUTH_API F8: only transfers made after the sender wallet was verified are credited
                    r = _db.one(_conn, """SELECT verified_at FROM wallets
                                           WHERE master_address = :a AND verified_at IS NOT NULL""", a=addr)
                    return r["verified_at"] if r else None

                outcome = credit_from_detection(det, treasury_address=treasury, min_topup_micro=min_topup,
                                                user_for_address=lookup, verified_at_for_address=verified_at)
                if outcome.credit is not None:
                    _book_credit(conn, ledger, outcome.credit, report)
                elif outcome.held is not None:
                    _book_held(conn, ledger, det, outcome.held, report, user_for_address=lookup)
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
        if failed_at is None:
            for req in requested:             # every transfer of the request was booked → served
                done = _db.rows(conn, """UPDATE deposit_scan_requests SET served_at = CAST(:n AS timestamptz)
                                          WHERE user_id = CAST(:u AS uuid) AND served_at IS NULL
                                            AND requested_at = CAST(:r AS timestamptz) RETURNING user_id""",
                                n=now, u=req["user_id"], r=req["requested_at"])
                if done:
                    report.scan_requests_served += 1
                else:                         # re-requested while this run was reading: serve it next run
                    report.scan_requests_deferred += 1
        if treasury_ok:
            if failed_at is not None:
                new_cur = max(0, failed_at - 1)
            else:
                # requested-wallet transfers never move the treasury cursor (its window may be behind them)
                new_cur = treasury_newest if treasury_newest is not None else now_ms - overlap_minutes * 60_000
                if report.complete:
                    new_cur = max(new_cur, now_ms - overlap_minutes * 60_000)
            _db.set_cursor(conn, JOB, treasury, new_cur, {"last_run_ms": now_ms, "booked_until_ms": booked_until,
                                                          "complete": report.complete})
    _db.log.info("deposits_scan_done", extra={"fields": report.as_dict()})
    return report.as_dict()


def _serve_requests(db: Any, info: Any, pacer: WeightPacer, treasury: str, now_ms: int, limit: int,
                    report: DepositsReport) -> tuple[list[dict], list[dict]]:
    """Read the ledger window of every verified wallet of the oldest pending scan requests (budgeted). Returns
    (requests fully read — to mark served once booked, their ledger updates). A request whose wallets could not all
    be read (budget / deadline / HL error) stays pending (``scan_requests_deferred``)."""
    if limit <= 0:
        return [], []
    with _db.transaction(db) as conn:
        if _db.one(conn, "SELECT to_regclass('public.deposit_scan_requests') IS NOT NULL AS ok")["ok"] is not True:
            return [], []                     # before 0012
        pending = _db.rows(conn, """
            SELECT r.user_id::text AS user_id, r.requested_at, r.since,
                   (SELECT coalesce(array_agg(w.master_address ORDER BY w.verified_at), ARRAY[]::text[])
                      FROM wallets w WHERE w.user_id = r.user_id AND w.verified_at IS NOT NULL) AS wallets
              FROM deposit_scan_requests r
             WHERE r.served_at IS NULL
             ORDER BY r.requested_at, r.user_id
             LIMIT CAST(:n AS integer)""", n=int(limit))
    served: list[dict] = []
    out: list[dict] = []
    stop = False
    for req in pending:
        if stop:
            report.scan_requests_deferred += 1
            continue
        since_ms = max(0, _db.now_ms(_as_dt(req["since"])))
        wallets = [str(w).lower() for w in (req.get("wallets") or [])][:MAX_WALLETS_PER_REQUEST]
        got: list[dict] = []
        ok = True
        for w in wallets:
            if w == treasury:
                continue
            cursor = since_ms
            try:
                for _ in range(REQUEST_MAX_PAGES):
                    est = list_weight(LEDGER_PAGE)
                    if not pacer.can_start(est):          # shared budget / deadline: leave it for the next run
                        ok, stop = False, True
                        break
                    pacer.spend(est)
                    report.requests += 1
                    page = info.user_non_funding_ledger_updates(w, cursor, now_ms)
                    pacer.settle(list_weight(len(page)), est)
                    got.extend(u for u in page if isinstance(u, dict))
                    times = [u.get("time") for u in page if isinstance(u, dict) and isinstance(u.get("time"), int)]
                    if len(page) < LEDGER_PAGE or not times or max(times) <= cursor:
                        break
                    cursor = max(times)
            except AppError as e:
                report.errors.append(f"request_fetch:{type(e).__name__}")
                ok = False
                stop = True                               # budget exhausted or HL failing: stop serving requests
            if not ok:
                break
        if ok:
            served.append(req)
            out.extend(got)
        else:
            report.scan_requests_deferred += 1
    return served, out


def _as_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T", 1))


def _book_credit(conn: Any, ledger: Any, instr: Any, report: DepositsReport) -> None:
    from app.api.store import SqlStore                 # the same deposits upsert as the API's confirm path

    existing = ledger.get_transaction(conn, instr.idempotency_key)
    if existing is not None and existing.kind != instr.kind:
        # booked to suspense earlier (e.g. the wallet was verified after the transfer): never book twice
        report.already_booked += 1
        _now_attributable(conn, instr.external_ref, instr.amount_micro, instr.user_id, existing.kind)
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


def _now_attributable(conn: Any, tx_hash: str, amount_micro: int, user_id: str, booked_as: str) -> None:
    """Ops event: a transfer booked to suspense earlier now has a VERIFIED sender. Never credited automatically (the
    key is taken; REVIEW_AUTH_API F8) — ops attribute it by the maker-checker release (RUNBOOK §13.3)."""
    _db.ops_alert(conn, "topup_held_now_attributable", {
        "hash": tx_hash[:18], "amount_micro": int(amount_micro), "user_id": user_id, "booked_as": booked_as},
        severity="warn", dedup_key=f"topup_held_now_attributable:{tx_hash}")


def _book_held(conn: Any, ledger: Any, det: Any, reason: str, report: DepositsReport, *,
               user_for_address: Any = None) -> None:
    amount = int(det.amount_micro)
    if amount <= 0:
        report.ignored += 1
        return
    existing = ledger.get_transaction(conn, f"usdc_hl:{det.hash}")
    if existing is not None and existing.kind != "deposit_held":
        report.already_booked += 1                    # already credited to a user (e.g. via the API confirm path)
        return
    if existing is not None and user_for_address is not None:
        # already held: if it was held because the sender was unknown and that sender has since verified the wallet
        # (a later verification makes the transfer "predate the verification", so it stays held), tell ops once
        _held_sender_verified(conn, ledger, det, amount, user_for_address)
    ledger.ensure_account(conn, SUSPENSE_ACCOUNT, "liability", None, non_negative=False)
    tx = ledger.post_transaction(conn, f"usdc_hl:{det.hash}", "deposit_held",
                                 f"USDC held for review {det.hash[:10]}… ({reason[:60]})",
                                 [(TREASURY_ACCOUNT, amount), (SUSPENSE_ACCOUNT, -amount)], CREATED_BY)
    _record_held(conn, det, amount, reason, str(tx.id))
    if tx.created:
        report.held += 1
        report.held_micro += amount
    else:
        report.already_booked += 1


def _held_sender_verified(conn: Any, ledger: Any, det: Any, amount: int, user_for_address: Any) -> None:
    sender = str(getattr(det, "user_address", "") or "").lower()
    if not (len(sender) == 42 and sender.startswith("0x")):
        return
    user_id = user_for_address(sender)
    if not user_id:
        return
    if ledger.get_transaction(conn, f"suspense_release:{str(det.hash).lower()}") is not None:
        return                                        # already released (attributed or refunded)
    if _db.one(conn, "SELECT to_regclass('public.usdc_held_deposits') IS NOT NULL AS ok")["ok"] is True:
        rec = _db.one(conn, "SELECT reason FROM usdc_held_deposits WHERE tx_hash = :h", h=str(det.hash).lower())
        if rec is not None and not str(rec["reason"]).startswith("sender is not a verified user wallet"):
            return                                    # held for another reason (below minimum, …): not news
    _now_attributable(conn, str(det.hash), amount, str(user_id), "deposit_held")


def _record_held(conn: Any, det: Any, amount: int, reason: str, held_tx_id: str) -> None:
    """``usdc_held_deposits`` (0009): the ON-CHAIN sender of a held transfer, so an admin refund (RUNBOOK §13.3,
    admin ``held-deposits``) goes back to exactly that address. Same transaction as the ledger posting; idempotent."""
    if _db.one(conn, "SELECT to_regclass('public.usdc_held_deposits') IS NOT NULL AS ok")["ok"] is not True:
        return                                        # before 0009: the admin supplies + on-chain verifies the sender
    sender = str(getattr(det, "user_address", "") or "").lower()
    if not (len(sender) == 42 and sender.startswith("0x")):
        return
    _db.rows(conn, f"""
        INSERT INTO usdc_held_deposits (tx_hash, sender_address, amount_micro, reason, transfer_time, held_tx_id)
        VALUES (:h, :s, :a, :r, {_db.ms_to_ts('CAST(:t AS bigint)')}, CAST(:tx AS uuid))
        ON CONFLICT (tx_hash) DO NOTHING RETURNING tx_hash""",
             h=str(det.hash).lower(), s=sender, a=int(amount), r=(reason or "held")[:200], t=int(det.time),
             tx=held_tx_id)
