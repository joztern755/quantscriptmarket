"""Held USDC deposits: maker-checker release of ``suspense:usdc_unattributed`` (RUNBOOK §13.3; admin console
"Held deposits"; routes in app/api/routers/admin.py). Pure business logic over ``Services`` ports and an open
transaction ``conn`` — no FastAPI / pydantic here, so it is testable against a real database without the web stack.

A held transfer = the deposits-scan posting kind ``deposit_held`` (key ``usdc_hl:{hash}``: debit treasury:hl_usdc /
credit suspense). Its on-chain SENDER comes from ``usdc_held_deposits`` (recorded by the scan) — or, for transfers
held before 0009, from the maker, verified on-chain by the route (``svc.hl.find_usd_send`` sender → treasury, same
hash and amount) before ``propose`` is called.

1. ``propose`` (admin A, step-up): ``attribute`` to a user — ONLY a user whose VERIFIED wallet is the sender (the
   user proves control of the sending address by the normal wallet verification; RUNBOOK §13.3) — or ``refund`` to
   the sender. One live proposal per transfer; the maker cannot be the beneficiary.
2. ``approve`` (admin B ≠ A, step-up; B cannot be the beneficiary either) → ONE ledger transaction, key
   ``suspense_release:{hash}``: attribute → suspense → ``user:{id}:fee_balance`` + a ``deposits`` row (usdc_hl,
   credited, withdrawable = USDC-funded) + the user's ``topup_credited`` alert; refund → suspense →
   ``refunds:usdc_pending``. ``reject`` (a different admin) ends the proposal; a new one may be made.
3. Refund only: an admin gets the treasury ``usdSend`` typed data (destination = the recorded sender, amount = the
   held amount), signs it with the hardware wallet in the browser, posts it to Hyperliquid, and records the tx hash
   (``record_refund_sent``) AFTER the route verified it on-chain → ``refunds:usdc_pending`` → ``treasury:hl_usdc``
   (key ``suspense_refund:{hash}:sent``).
Every step is audit-logged; every POST is Idempotency-Key protected by the route (``run_idempotent``).
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.api import ledger_ops
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

__all__ = ["list_held", "held_or_404", "propose", "approve", "reject", "refund_typed_data", "record_refund_sent",
           "release_out", "held_out"]

_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
ACTIONS = ("attribute", "refund")


def _require_refunds_enabled(svc: Any) -> None:
    """Owner (30 Sep 2026): refunds of held deposits are gated with payouts — no USDC leaves the treasury until
    PAYOUTS_ENABLED is on. Attribution to a verified user (no USDC leaves) stays available."""
    # same check and error as deps.require_payouts_enabled (not imported: deps pulls in FastAPI)
    if not svc.config.launch.payouts_enabled:
        raise Forbidden("withdrawals and payouts are not enabled yet", reason="payouts_disabled")


def norm_hash(tx_hash: str) -> str:
    h = str(tx_hash or "").strip().lower()
    if not _HASH_RE.fullmatch(h):
        raise ValidationFailed("bad transaction hash")
    return h


def _norm_addr(a: Optional[str]) -> Optional[str]:
    if a is None:
        return None
    s = str(a).strip().lower()
    if not _ADDR_RE.fullmatch(s):
        raise ValidationFailed("bad address")
    return s


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def held_out(r: dict) -> dict:
    return {"tx_hash": r["tx_hash"], "held_tx_id": str(r["held_tx_id"]), "amount_micro": int(r["amount_micro"]),
            "sender_address": r.get("sender_address"), "reason": r.get("reason"), "memo": r.get("memo"),
            "transfer_time": r.get("transfer_time"), "created_at": r["created_at"],
            "release_id": _s(r.get("release_id")), "release_status": r.get("release_status"),
            "release_action": r.get("release_action")}


def release_out(r: dict) -> dict:
    return {k: (_s(r.get(k)) if k in ("id", "held_tx_id", "user_id", "maker_admin", "checker_admin", "release_tx_id",
                                      "refund_ledger_tx_id", "sent_by") else r.get(k))
            for k in ("id", "created_at", "tx_hash", "held_tx_id", "amount_micro", "action", "user_id", "sender_address",
                      "sender_source", "evidence", "status", "maker_admin", "checker_admin", "decided_at",
                      "decision_reason", "release_tx_id", "refund_tx_hash", "refund_ledger_tx_id", "sent_by", "sent_at")}


def list_held(conn: Any, svc: Any, *, open_only: bool, limit: int, cursor: Any) -> list[dict]:
    return svc.store.list_held_deposits(conn, open_only=open_only, limit=limit, cursor=cursor)


def held_or_404(conn: Any, svc: Any, tx_hash: str) -> dict:
    held = svc.store.get_held_deposit(conn, norm_hash(tx_hash))
    if held is None:
        raise NotFound("no held USDC deposit with this transaction hash")
    return held


def propose(conn: Any, svc: Any, ctx: Any, *, tx_hash: str, action: str, user_id: Optional[str],
            sender_address: Optional[str], evidence: str, onchain_verified_sender: Optional[str] = None) -> dict:
    """Admin A proposes a release. ``onchain_verified_sender``: the sender the route verified on-chain (only used
    when the scan did not record one)."""
    if action not in ACTIONS:
        raise ValidationFailed("action must be attribute or refund")
    if action == "refund":
        _require_refunds_enabled(svc)
    h = norm_hash(tx_hash)
    held = held_or_404(conn, svc, h)
    if held.get("release_id") is not None:
        raise Conflict("a release for this deposit is already proposed or done", status=held.get("release_status"))
    provided = _norm_addr(sender_address)
    recorded = _norm_addr(held.get("sender_address"))
    if recorded is not None:
        if provided is not None and provided != recorded:
            raise ValidationFailed("sender_address differs from the on-chain sender recorded by the deposits scan")
        sender, source = recorded, "scan"
    else:
        verified = _norm_addr(onchain_verified_sender)
        if provided is None or verified is None or verified != provided:
            raise ValidationFailed("this deposit has no recorded sender: give sender_address; it must match the "
                                   "transfer on-chain", reason="sender_unverified")
        sender, source = provided, "onchain"
    uid: Optional[str] = None
    if action == "attribute":
        if not user_id:
            raise ValidationFailed("user_id is required to attribute a deposit")
        uid = str(user_id)
        if uid == ctx.user_id:
            raise Forbidden("you cannot attribute a held deposit to yourself")
        user = svc.store.get_user(conn, uid)
        if user is None:
            raise NotFound("user not found")
        if user.get("status") != "active":
            raise Conflict("the user is not active", status=user.get("status"))
        if svc.store.verified_wallet(conn, uid, sender) is None:
            raise Forbidden("the sending address is not a verified wallet of this user: they must verify it first "
                            "(signed message), or refund the deposit", reason="sender_not_verified_for_user")
    elif user_id:
        raise ValidationFailed("a refund goes back to the sender; do not pass user_id")
    row = svc.store.insert_suspense_release(conn, tx_hash=h, held_tx_id=str(held["held_tx_id"]),
                                            amount_micro=int(held["amount_micro"]), action=action, user_id=uid,
                                            sender_address=sender, sender_source=source, evidence=evidence,
                                            maker=ctx.user_id)
    if row is None:
        raise Conflict("a release for this deposit is already proposed or done")
    svc.audit.write(conn, actor=ctx.actor, action=f"suspense.propose.{action}", target=f"held_deposit:{h}",
                    payload={"release_id": str(row["id"]), "amount_micro": int(held["amount_micro"]), "user_id": uid,
                             "sender": sender, "sender_source": source, "evidence": evidence},
                    ip_hash=ctx.ip_hash)
    svc.notifier.notify(conn, user_id=None, severity="warn", kind="suspense_release_proposed",
                        payload={"release_id": str(row["id"]), "action": action, "amount_micro": int(held["amount_micro"]),
                                 "hash": h[:18]})
    return row


def _release_for_decision(conn: Any, svc: Any, ctx: Any, release_id: str) -> dict:
    rel = svc.store.get_suspense_release(conn, release_id, for_update=True)
    if rel is None or rel["status"] != "proposed":
        raise NotFound("no proposed release with this id")
    if str(rel["maker_admin"]) == ctx.user_id:
        raise Forbidden("the proposing admin cannot decide their own proposal")
    return rel


def approve(conn: Any, svc: Any, ctx: Any, *, release_id: str, reason: str) -> dict:
    """Admin B ≠ A approves → the ledger moves out of suspense (ONE transaction, key suspense_release:{hash})."""
    rel = _release_for_decision(conn, svc, ctx, release_id)
    if rel.get("user_id") is not None and str(rel["user_id"]) == ctx.user_id:
        raise Forbidden("you cannot approve a release to yourself")
    h = str(rel["tx_hash"])
    held = held_or_404(conn, svc, h)
    amount = int(rel["amount_micro"])
    if int(held["amount_micro"]) != amount or str(held["held_tx_id"]) != str(rel["held_tx_id"]):
        raise Conflict("the held deposit does not match this proposal")
    if rel["action"] == "attribute":
        uid = str(rel["user_id"])
        svc.store.lock_user(conn, uid)
        if svc.store.verified_wallet(conn, uid, str(rel["sender_address"])) is None:
            raise Conflict("the sending address is no longer a verified wallet of this user")
        tx = ledger_ops.release_suspense_to_user(conn, svc, tx_hash=h, user_id=uid, amount=amount, actor=ctx.actor)
        svc.store.mark_deposit_credited(conn, user_id=uid, method="usdc_hl", external_ref=h, amount_micro=amount,
                                        tx_id=tx, withdrawable=True,
                                        meta={"source": "suspense_release", "release_id": str(rel["id"])})
        svc.notifier.notify(conn, user_id=uid, severity="info", kind="topup_credited",
                            payload={"amount_micro": amount, "method": "USDC (held deposit released)"})
    else:
        _require_refunds_enabled(svc)
        tx = ledger_ops.release_suspense_to_refund(conn, svc, tx_hash=h, amount=amount, actor=ctx.actor)
    row = svc.store.approve_suspense_release(conn, release_id, checker=ctx.user_id, now=svc.now(), reason=reason,
                                             release_tx_id=tx)
    if row is None:
        raise Conflict("release state changed; reload")
    svc.audit.write(conn, actor=ctx.actor, action=f"suspense.approve.{rel['action']}", target=f"held_deposit:{h}",
                    payload={"release_id": str(rel["id"]), "maker": str(rel["maker_admin"]), "ledger_tx": tx,
                             "amount_micro": amount, "reason": reason}, ip_hash=ctx.ip_hash)
    return row


def reject(conn: Any, svc: Any, ctx: Any, *, release_id: str, reason: str) -> dict:
    rel = _release_for_decision(conn, svc, ctx, release_id)
    row = svc.store.reject_suspense_release(conn, release_id, checker=ctx.user_id, now=svc.now(), reason=reason)
    if row is None:
        raise Conflict("release state changed; reload")
    svc.audit.write(conn, actor=ctx.actor, action=f"suspense.reject.{rel['action']}", target=f"held_deposit:{rel['tx_hash']}",
                    payload={"release_id": str(rel["id"]), "reason": reason}, ip_hash=ctx.ip_hash)
    return row


def _approved_refund(conn: Any, svc: Any, release_id: str, *, for_update: bool) -> dict:
    rel = svc.store.get_suspense_release(conn, release_id, for_update=for_update)
    if rel is None:
        raise NotFound("release not found")
    if rel["action"] != "refund" or rel["status"] != "approved":
        raise Conflict("only an approved refund can be sent", status=rel["status"], action=rel["action"])
    return rel


def refund_typed_data(conn: Any, svc: Any, ctx: Any, *, release_id: str, signature_chain_id: str) -> tuple[dict, dict]:
    """(release, usdSend typed-data payload) for the hardware treasury wallet. Destination = the recorded sender."""
    from app.api.validation import micro_to_usd_string

    _require_refunds_enabled(svc)
    rel = _approved_refund(conn, svc, release_id, for_update=False)
    svc.audit.write(conn, actor=ctx.actor, action="suspense.refund.typed_data", target=f"held_deposit:{rel['tx_hash']}",
                    payload={"release_id": str(rel["id"])}, ip_hash=ctx.ip_hash)
    payload = svc.typed_data.usd_send(destination=str(rel["sender_address"]),
                                      amount=micro_to_usd_string(int(rel["amount_micro"])),
                                      time_ms=int(svc.now().timestamp() * 1000), signature_chain_id=signature_chain_id)
    return rel, payload


def check_refund_sendable(conn: Any, svc: Any, *, release_id: str, refund_tx_hash: str) -> dict:
    """Pre-check before the route verifies the transfer on-chain (outside any transaction)."""
    rel = _approved_refund(conn, svc, release_id, for_update=False)
    if svc.store.tx_hash_used(conn, norm_hash(refund_tx_hash)):
        raise Conflict("this transaction hash is already recorded")
    return rel


def record_refund_sent(conn: Any, svc: Any, ctx: Any, *, release_id: str, refund_tx_hash: str,
                       time_ms: Optional[int] = None) -> dict:
    """The route has verified on-chain: treasury → recorded sender, this hash, exactly the held amount."""
    _require_refunds_enabled(svc)
    rh = norm_hash(refund_tx_hash)
    rel = _approved_refund(conn, svc, release_id, for_update=True)
    if svc.store.tx_hash_used(conn, rh):
        raise Conflict("this transaction hash is already recorded")
    amount = int(rel["amount_micro"])
    tx = ledger_ops.settle_suspense_refund(conn, svc, tx_hash=str(rel["tx_hash"]), refund_tx_hash=rh, amount=amount,
                                           actor=ctx.actor)
    row = svc.store.mark_suspense_refund_sent(conn, release_id, refund_tx_hash=rh, ledger_tx_id=tx, admin=ctx.user_id,
                                              now=svc.now())
    if row is None:
        raise Conflict("release state changed; reload")
    svc.audit.write(conn, actor=ctx.actor, action="suspense.refund.sent", target=f"held_deposit:{rel['tx_hash']}",
                    payload={"release_id": str(rel["id"]), "tx_hash": rh, "ledger_tx": tx, "amount_micro": amount,
                             "to": str(rel["sender_address"]), "time_ms": time_ms}, ip_hash=ctx.ip_hash)
    svc.notifier.notify(conn, user_id=None, severity="info", kind="suspense_refund_sent",
                        payload={"release_id": str(rel["id"]), "amount_micro": amount, "tx_hash": rh})
    return row
