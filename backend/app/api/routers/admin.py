"""Admin console API. Role from OUR DB (never token claims); every mutation requires step-up and is audit-logged.

Maker-checker policy (SPEC §5.6 "all admin actions maker-checker"):
  * PROTECTIVE actions take effect immediately with one admin (speed matters in an incident): engaging a kill
    switch / pausing entries, suspending a user, pausing or delisting a strategy (its live subscriptions go
    reduce_only), rejecting a payout, rejecting a strategy back to draft.
  * PERMISSIVE actions need a second, different admin: lifting a switch (system_flags.pending_*), listing a
    strategy version, setting an in-house price, un-suspending a user (admin_changes table), approving payouts
    (withdrawals/payouts maker_admin ≠ checker_admin, enforced by DB CHECKs too; neither may be the beneficiary).
  * Creator KYC (owner decision 30 Sep 2026): ONE admin decides, step-up + audit-logged, never via admin_changes.
    Manual provider: pending/rejected → approved. Sumsub: only a provider GREEN (``provider_approved``) can be
    confirmed → approved. Rejection is immediate. An admin cannot approve their own KYC.
Held USDC deposits (suspense:usdc_unattributed, RUNBOOK §13.3): maker-checker release (/admin/held-deposits…;
logic in app/api/suspense.py) — attribute to the user whose verified wallet sent it, or refund to the sender with a
hardware-wallet usdSend recorded like a payout. Refunds are NOT gated by PAYOUTS_ENABLED (it returns the sender's own
money; still two admins + hardware wallet + on-chain verification).
Trusted builder dexes (SPEC §12, owner 30 Sep 2026; REVIEW_TRADING_KEYS F1): ONE admin adds a dex (step-up,
audit-logged, ops alert); removal is immediate (protective) and pauses new entries on the dex's markets from the next
executor tick (exits keep running). Listing proposals and approvals re-check every market against the allowlist.
Payout execution: after approved_2 an admin requests the usdSend typed data, signs it in the browser with the
hardware treasury wallet, posts it to Hyperliquid, then records the tx hash here; we verify it on-chain before
settling the ledger hold against treasury:hl_usdc.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api import billing_ops, ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    admin_step_up,
    admin_user,
    decode_cursor_or_422,
    get_services,
    idempotency_key,
    next_cursor,
    require_payouts_enabled,
    run_idempotent,
    user_limit,
)
from app.api.routers.alerts import alert_out
from app.api.routers.creator import history_days_for, require_trusted_markets, version_out
from app.api.validation import micro_to_usd_string
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[user_limit("admin", 120, 60)])

PayoutKind = Literal["withdrawal", "payout"]
SEND_REJECT_AFTER = timedelta(hours=72)   # HL usdSend nonce window: after it a signed payload can no longer execute


def deps_hash(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def _flag_key(key: str) -> str:
    import re
    if not re.match(S.FLAG_KEY_PATTERN, key):
        raise ValidationFailed("unknown flag key")
    return key


def _change_out(r: dict) -> S.ChangeOut:
    return S.ChangeOut(id=r["id"], kind=r["kind"], target=r["target"], payload=r.get("payload") or {},
                       reason=r["reason"], status=r["status"], maker_admin=r["maker_admin"],
                       checker_admin=r.get("checker_admin"), created_at=r["created_at"], decided_at=r.get("decided_at"))


def _propose(conn: Any, svc: Services, ctx: AuthCtx, *, kind: str, target: str, payload: dict,
             reason: str) -> S.AdminActionOut:
    if svc.store.open_change_for(conn, kind, target) is not None:
        raise Conflict("a change for this target is already pending")
    row = svc.store.insert_change(conn, kind=kind, target=target, payload=payload, reason=reason, maker=ctx.user_id)
    svc.audit.write(conn, actor=ctx.actor, action=f"admin.propose.{kind}", target=target,
                    payload={"change_id": str(row["id"]), **payload, "reason": reason}, ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="pending", change=_change_out(row))


# ============================================================================================ flags / kill switches
@router.get("/flags", response_model=list[S.FlagOut])
def list_flags(ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> list[S.FlagOut]:
    with svc.db.begin() as conn:
        rows = svc.store.list_flags(conn)
    return [S.FlagOut(key=r["key"], value=r["value"], pending_value=r.get("pending_value"),
                      pending_by=r.get("pending_by"), pending_at=r.get("pending_at"), updated_by=r.get("updated_by"),
                      updated_at=r.get("updated_at")) for r in rows]


@router.post("/flags", response_model=S.AdminActionOut)
def set_flag(body: S.FlagSetIn, ctx: AuthCtx = Depends(admin_step_up),
             svc: Services = Depends(get_services)) -> S.AdminActionOut:
    """value=true (engage / pause) applies NOW; value=false (lift) is proposed and needs a second admin."""
    with svc.db.begin() as conn:
        current = svc.store.get_flag(conn, body.key, for_update=True)
        if body.value:
            svc.store.set_flag(conn, body.key, True, ctx.actor)
            svc.notifier.notify(conn, user_id=None, severity="critical", kind="kill_switch_engaged",
                                payload={"key": body.key, "by": ctx.user_id, "reason": body.reason})
            svc.audit.write(conn, actor=ctx.actor, action="flag.engage", target=f"flag:{body.key}",
                            payload={"reason": body.reason}, ip_hash=ctx.ip_hash)
            return S.AdminActionOut(status="applied")
        if current is None or current.get("value") is not True:
            raise Conflict("this switch is not engaged")
        if not svc.store.propose_flag(conn, body.key, False, ctx.actor, svc.now()):
            raise Conflict("a change to this switch is already pending")
        svc.audit.write(conn, actor=ctx.actor, action="flag.lift.propose", target=f"flag:{body.key}",
                        payload={"reason": body.reason}, ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="pending")


@router.post("/flags/{key}/approve", response_model=S.AdminActionOut)
def approve_flag(body: S.DecisionIn, key: str = Path(..., max_length=80), ctx: AuthCtx = Depends(admin_step_up),
                 svc: Services = Depends(get_services)) -> S.AdminActionOut:
    key = _flag_key(key)
    with svc.db.begin() as conn:
        flag = svc.store.get_flag(conn, key, for_update=True)
        if flag is None or flag.get("pending_by") is None:
            raise NotFound("no pending change for this switch")
        if flag["pending_by"] == ctx.actor:
            raise Forbidden("the proposing admin cannot approve their own change")
        svc.store.set_flag(conn, key, flag["pending_value"], ctx.actor)
        svc.notifier.notify(conn, user_id=None, severity="warn", kind="kill_switch_lifted",
                            payload={"key": key, "maker": flag["pending_by"], "checker": ctx.actor})
        svc.audit.write(conn, actor=ctx.actor, action="flag.lift.approve", target=f"flag:{key}",
                        payload={"maker": flag["pending_by"], "value": flag["pending_value"], "reason": body.reason},
                        ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied")


@router.post("/flags/{key}/reject", response_model=S.AdminActionOut)
def reject_flag(body: S.DecisionIn, key: str = Path(..., max_length=80), ctx: AuthCtx = Depends(admin_step_up),
                svc: Services = Depends(get_services)) -> S.AdminActionOut:
    key = _flag_key(key)
    with svc.db.begin() as conn:
        if not svc.store.clear_flag_proposal(conn, key):
            raise NotFound("no pending change for this switch")
        svc.audit.write(conn, actor=ctx.actor, action="flag.lift.reject", target=f"flag:{key}",
                        payload={"reason": body.reason}, ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied")


# ============================================================================================ generic change queue
@router.get("/changes", response_model=S.Page[S.ChangeOut])
def list_changes(status: Optional[Literal["pending", "approved", "rejected", "cancelled"]] = Query("pending"),
                 limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                 ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> S.Page[S.ChangeOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_changes(conn, status, limit, cur), limit)
    return S.Page[S.ChangeOut](items=[_change_out(r) for r in rows], next_cursor=nxt)


def _require_trusted_or_conflict(conn: Any, svc: Services, markets: list[str]) -> None:
    """SPEC §12: a strategy version is listed only when every market is a validator perp or on a trusted dex."""
    try:
        require_trusted_markets(conn, svc, markets, what="strategy markets")
    except ValidationFailed as e:
        raise Conflict(e.message, **(e.details if isinstance(e.details, dict) else {})) from None


_LIST_SNAPSHOT = ("price_monthly_micro", "profit_share_bps", "owner_user_id")


def _strategy_snapshot(st: dict) -> dict:
    """The terms a listing proposal was reviewed with (pinned in the immutable admin_changes payload; F6)."""
    return {k: (str(st[k]) if k == "owner_user_id" and st.get(k) is not None else st.get(k)) for k in _LIST_SNAPSHOT}


def _apply_change(conn: Any, svc: Services, ctx: AuthCtx, ch: dict) -> None:
    """Re-validates the CURRENT state at approval time (REVIEW_AUTH_API F6): a change reviewed against a state that no
    longer exists is refused with 409 (reject it and propose again)."""
    kind, payload = ch["kind"], ch.get("payload") or {}
    if kind == "strategy_list":
        sid, vid = ch["target"].split(":", 1)[1], str(payload["version_id"])
        st = svc.store.get_strategy(conn, sid, for_update=True)
        ver = svc.store.get_version(conn, vid)
        if st is None or ver is None or str(ver["strategy_id"]) != sid:
            raise Conflict("strategy or version no longer exists")
        if st["status"] not in ("review", "listed", "paused"):
            raise Conflict("the strategy is no longer reviewable (delisted or back to draft)", status=st["status"],
                           reason="stale_change")
        pinned = payload.get("terms")
        if not isinstance(pinned, dict) or _strategy_snapshot(st) != pinned:
            raise Conflict("the strategy's terms changed since this listing was proposed; propose again",
                           reason="terms_changed", proposed=pinned, current=_strategy_snapshot(st))
        if st["price_monthly_micro"] is None or st["profit_share_bps"] is None:
            raise Conflict("set a price and profit share before listing")
        _require_trusted_or_conflict(conn, svc, list(ver.get("markets") or st["markets"] or []))
        if not st["in_house"]:
            kyc = svc.store.get_kyc(conn, str(st["owner_user_id"]))
            if not kyc or kyc["status"] != "approved":
                raise Conflict("the creator's KYC is not approved")
            # SPEC §12 (owner): ≥ risk.min_listing_history_days (180) of backtestable history to list; versions
            # under short_history_warning_days (365) list with a "Short history (N days)" warning instead.
            bt = ver.get("backtest") or {}
            days = history_days_for(svc, list(ver.get("markets") or st["markets"] or []), ver.get("timeframe") or st["timeframe"], bt)
            recorded = bt.get("history_days")
            if isinstance(recorded, int) and not isinstance(recorded, bool):
                days = max(days or 0, recorded)
            days = days or 0
            min_days = svc.settings.risk.min_listing_history_days
            if float(days) < min_days:
                raise Conflict(f"backtest covers less than {min_days} days of history; cannot list",
                               history_days=days, min_days=min_days)
        svc.store.publish_version(conn, vid, svc.now())
        svc.store.set_strategy_status(conn, sid, "listed")
    elif kind == "strategy_price":
        sid = ch["target"].split(":", 1)[1]
        st = svc.store.get_strategy(conn, sid, for_update=True)
        if st is None or st["status"] == "delisted":
            raise Conflict("the strategy no longer exists or is delisted", reason="stale_change")
        if st.get("price_monthly_micro") != payload.get("previous_micro"):
            raise Conflict("the price changed since this was proposed; propose again", reason="stale_change")
        # applies to NEW subscriptions only: existing ones keep the price pinned at subscribe (M8)
        svc.store.set_strategy_price(conn, sid, int(payload["price_monthly_micro"]))
    elif kind == "user_unsuspend":
        uid = ch["target"].split(":", 1)[1]
        user = svc.store.get_user(conn, uid)
        if user is None or user["status"] != "suspended":
            raise Conflict("the user is not suspended any more", reason="stale_change")
        svc.store.set_user_status(conn, uid, "active")
    elif kind == "user_suspend":   # suspending another ADMIN (F12): second admin required
        uid = ch["target"].split(":", 1)[1]
        if uid == ctx.user_id:
            raise Forbidden("an admin cannot approve their own suspension")
        user = svc.store.get_user(conn, uid)
        if user is None or user["status"] != "active":
            raise Conflict("the user is not active any more", reason="stale_change")
        svc.store.set_user_status(conn, uid, "suspended")
    elif kind == "kyc_approve":   # legacy queue entries: KYC is a single-admin decision now (POST /users/{id}/kyc)
        raise Conflict("KYC approval no longer uses the change queue; reject this entry and use the KYC decision")
    else:
        raise Conflict("unknown change kind")


@router.post("/changes/{change_id}/approve", response_model=S.AdminActionOut)
def approve_change(change_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                   svc: Services = Depends(get_services)) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        ch = svc.store.get_change(conn, str(change_id), for_update=True)
        if ch is None or ch["status"] != "pending":
            raise NotFound("no pending change with this id")
        if str(ch["maker_admin"]) == ctx.user_id:
            raise Forbidden("the proposing admin cannot approve their own change")
        _apply_change(conn, svc, ctx, ch)
        row = svc.store.decide_change(conn, str(change_id), status="approved", checker=ctx.user_id, now=svc.now(),
                                      decision_reason=body.reason)
        if row is None:
            raise Conflict("change state changed; reload")
        svc.audit.write(conn, actor=ctx.actor, action=f"admin.approve.{ch['kind']}", target=ch["target"],
                        payload={"change_id": str(change_id), "maker": str(ch["maker_admin"]), "reason": body.reason},
                        ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied", change=_change_out(row))


@router.post("/changes/{change_id}/reject", response_model=S.AdminActionOut)
def reject_change(change_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                  svc: Services = Depends(get_services)) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        ch = svc.store.get_change(conn, str(change_id), for_update=True)
        if ch is None or ch["status"] != "pending":
            raise NotFound("no pending change with this id")
        if str(ch["maker_admin"]) == ctx.user_id:
            raise Forbidden("ask another admin to reject (or leave it pending)")
        row = svc.store.decide_change(conn, str(change_id), status="rejected", checker=ctx.user_id, now=svc.now(),
                                      decision_reason=body.reason)
        if row is None:
            raise Conflict("change state changed; reload")
        svc.audit.write(conn, actor=ctx.actor, action=f"admin.reject.{ch['kind']}", target=ch["target"],
                        payload={"change_id": str(change_id), "reason": body.reason}, ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied", change=_change_out(row))


# ============================================================================================ payouts / withdrawals
def _admin_payout_out(r: dict, conn: Any = None, svc: Optional[Services] = None) -> S.AdminPayoutOut:
    """With (conn, svc): what the approving admins must see (REVIEW_AUTH_API F5) — destination wallet age, the
    beneficiary's security hold and recent security events, and the hold reasons that block the second approval."""
    extra: dict[str, Any] = {}
    if conn is not None and svc is not None and r.get("status") in ("requested", "approved_1", "approved_2"):
        reasons, pctx = billing_ops.payout_holds(conn, svc, user_id=str(r["beneficiary"]), to_address=r["to_address"])
        verified = pctx.get("to_address_verified_at")
        extra = dict(to_address_verified_at=verified,
                     wallet_age_hours=int((svc.now() - verified).total_seconds() // 3600) if verified else None,
                     security_hold_until=pctx.get("security_hold_until"), hold_reasons=reasons,
                     recent_security_events=[S.SecurityEventOut(action=e["action"], at=e["created_at"])
                                             for e in pctx.get("events") or []])
    return S.AdminPayoutOut(id=r["id"], kind=r["kind"], beneficiary=r["beneficiary"],
                            amount_micro=int(r["amount_micro"]), to_address=r["to_address"], status=r["status"],
                            maker_admin=r.get("maker_admin"), checker_admin=r.get("checker_admin"),
                            tx_hash=r.get("tx_hash"), created_at=r["created_at"],
                            send_issued_at=r.get("send_issued_at"), **extra)


def _source_account(conn: Any, svc: Services, kind: str, row: dict) -> str:
    if kind == "withdrawal":
        return ledger_ops.fee_balance(str(row["beneficiary"]))
    code = svc.store.account_code_by_id(conn, str(row["ledger_account_id"]))
    if code is None:
        raise Conflict("payout ledger account missing")
    return code


@router.get("/payouts", response_model=S.Page[S.AdminPayoutOut])
def list_payouts(kind: PayoutKind = Query("withdrawal"),
                 status: Optional[Literal["requested", "approved_1", "approved_2", "sent", "rejected"]] = Query(None),
                 limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                 ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> S.Page[S.AdminPayoutOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.admin_list_payouts(conn, kind=kind, status=status, limit=limit, cursor=cur),
                                limit)
        items = [_admin_payout_out(r, conn, svc) for r in rows]
        svc.audit.write(conn, actor=ctx.actor, action="admin.read.payouts", target="",
                        payload={"kind": kind, "status": status, "n": len(items)}, ip_hash=ctx.ip_hash)
    return S.Page[S.AdminPayoutOut](items=items, next_cursor=nxt)


@router.post("/payouts/{kind}/{payout_id}/approve", response_model=S.AdminPayoutOut)
def approve_payout(kind: PayoutKind, payout_id: UUID, ctx: AuthCtx = Depends(admin_step_up),
                   svc: Services = Depends(get_services)) -> S.AdminPayoutOut:
    require_payouts_enabled(svc)
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id))
        if row is None:
            raise NotFound("not found")
        if str(row["beneficiary"]) == ctx.user_id:
            raise Forbidden("you cannot approve a payout to yourself")
        now = svc.now()
        if row["status"] == "requested":
            ok, step = svc.store.payout_approve_1(conn, kind, str(payout_id), ctx.user_id, now), "approve_1"
        elif row["status"] == "approved_1":
            if str(row.get("maker_admin")) == ctx.user_id:
                raise Forbidden("the second approval must come from a different admin")
            # F5: never release money to a fresh wallet or an account on a security hold
            reasons, _ = billing_ops.payout_holds(conn, svc, user_id=str(row["beneficiary"]),
                                                  to_address=row["to_address"])
            if reasons:
                raise Conflict("the beneficiary is on a security hold; approve after it ends", reason=reasons[0],
                               hold_reasons=reasons)
            ok, step = svc.store.payout_approve_2(conn, kind, str(payout_id), ctx.user_id, now), "approve_2"
        else:
            raise Conflict("nothing to approve in this state", status=row["status"])
        if not ok:
            raise Conflict("state changed; reload")
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.{step}", target=f"{kind}:{payout_id}",
                        payload={"amount_micro": int(row["amount_micro"])}, ip_hash=ctx.ip_hash)
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
        return _admin_payout_out(row, conn, svc)


@router.post("/payouts/{kind}/{payout_id}/reject", response_model=S.AdminPayoutOut)
def reject_payout(kind: PayoutKind, payout_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                  svc: Services = Depends(get_services)) -> S.AdminPayoutOut:
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id))
        if row is None:
            raise NotFound("not found")
        if row["status"] not in ("requested", "approved_1", "approved_2"):
            raise Conflict("cannot reject in this state", status=row["status"])
        issued = row.get("send_issued_at")
        if issued is not None and svc.now() - issued < SEND_REJECT_AFTER:
            # M4: the signed usdSend may still execute on Hyperliquid — rejecting now could pay twice
            raise Conflict("the transfer was prepared for signing; record it as sent, or reject after 72 h",
                           reason="send_in_progress", reject_after=(issued + SEND_REJECT_AFTER).isoformat())
        source = _source_account(conn, svc, kind, row)
        tx = ledger_ops.release_hold(conn, svc, kind=kind, row=row, source_account=source, actor=ctx.actor)
        if not svc.store.payout_reject(conn, kind, str(payout_id), ctx.user_id, body.reason):
            raise Conflict("state changed; reload")
        svc.notifier.notify(conn, user_id=str(row["beneficiary"]), severity="info", kind=f"{kind}_rejected",
                            payload={"amount_micro": int(row["amount_micro"]), "request_id": str(payout_id)[:8]})
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.reject", target=f"{kind}:{payout_id}",
                        payload={"reason": body.reason, "release_tx": tx}, ip_hash=ctx.ip_hash)
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
    return _admin_payout_out(row)


@router.post("/payouts/{kind}/{payout_id}/typed-data", response_model=S.PayoutTypedDataOut)
def payout_typed_data(kind: PayoutKind, payout_id: UUID, body: S.PayoutTypedDataIn,
                      ctx: AuthCtx = Depends(admin_step_up), svc: Services = Depends(get_services)) -> S.PayoutTypedDataOut:
    require_payouts_enabled(svc)
    """Issued ONCE per payout (M4): the usdSend nonce (= time ms) is pinned on the row at the first call and every
    later call returns the same payload, so two signatures can never become two transfers (Hyperliquid refuses a
    reused nonce). A payout with issued typed data cannot be rejected for 72 h."""
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id))
        if row is None:
            raise NotFound("not found")
        if row["status"] != "approved_2":
            raise Conflict("needs two approvals first", status=row["status"])
        sent = svc.store.set_payout_send(conn, kind, str(payout_id), nonce=int(svc.now().timestamp() * 1000),
                                         admin_id=ctx.user_id, now=svc.now())
        if sent is None:
            raise Conflict("state changed; reload")
        nonce = int(sent["send_nonce"])
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.typed_data", target=f"{kind}:{payout_id}",
                        payload={"nonce": nonce, "first_issue": row.get("send_nonce") is None}, ip_hash=ctx.ip_hash)
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
    payload = svc.typed_data.usd_send(destination=row["to_address"], amount=micro_to_usd_string(int(row["amount_micro"])),
                                      time_ms=nonce, signature_chain_id=body.signature_chain_id)
    return S.PayoutTypedDataOut(payout=_admin_payout_out(row), payload=payload,
                                exchange_url=svc.settings.hl_api_url.rstrip("/") + "/exchange")


@router.post("/payouts/{kind}/{payout_id}/sent", response_model=S.AdminPayoutOut)
def payout_sent(kind: PayoutKind, payout_id: UUID, body: S.PayoutSentIn, ctx: AuthCtx = Depends(admin_step_up),
                svc: Services = Depends(get_services)) -> S.AdminPayoutOut:
    require_payouts_enabled(svc)
    tx_hash = body.tx_hash.lower()
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
        if row is None:
            raise NotFound("not found")
        if row["status"] != "approved_2":
            raise Conflict("needs two approvals first", status=row["status"])
        if svc.store.tx_hash_used(conn, tx_hash):
            raise Conflict("this transaction hash is already recorded")
    if not svc.hl.find_usd_send(sender=svc.settings.treasury_address, destination=row["to_address"],
                                amount_micro=int(row["amount_micro"]), tx_hash=tx_hash):
        raise ValidationFailed("transfer not found on Hyperliquid for this amount and destination (yet)")
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id))
        if row is None or row["status"] != "approved_2":
            raise Conflict("state changed; reload")
        # F9/M4: one on-chain transfer settles ONE row, re-checked inside the settling transaction (PK + unique index)
        if svc.store.tx_hash_used(conn, tx_hash) or not svc.store.claim_payout_tx_hash(conn, tx_hash, kind,
                                                                                        str(payout_id)):
            raise Conflict("this transaction hash is already recorded")
        tx = ledger_ops.settle_sent(conn, svc, kind=kind, row=row, tx_hash=tx_hash, actor=ctx.actor)
        if not svc.store.payout_mark_sent(conn, kind, str(payout_id), tx_hash, tx):
            raise Conflict("state changed; reload")
        svc.notifier.notify(conn, user_id=str(row["beneficiary"]), severity="info", kind=f"{kind}_sent",
                            payload={"amount_micro": int(row["amount_micro"]), "request_id": str(payout_id)[:8],
                                     "tx_hash": tx_hash})
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.sent", target=f"{kind}:{payout_id}",
                        payload={"tx_hash": tx_hash, "ledger_tx": tx, "time_ms": body.time_ms}, ip_hash=ctx.ip_hash)
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
    return _admin_payout_out(row)


# ============================================================================================ strategies
@router.get("/strategies", response_model=S.Page[S.AdminStrategyOut])
def list_strategies(status: Optional[Literal["draft", "review", "listed", "paused", "delisted"]] = Query("review"),
                    limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                    ctx: AuthCtx = Depends(admin_user),
                    svc: Services = Depends(get_services)) -> S.Page[S.AdminStrategyOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.admin_list_strategies(conn, status, limit, cur), limit)
        items = [S.AdminStrategyOut(
            id=r["id"], slug=r["slug"], name=r["name"], status=r["status"], in_house=r["in_house"],
            owner_user_id=r.get("owner_user_id"), owner_kyc_status=r.get("owner_kyc_status"),
            price_monthly_micro=r.get("price_monthly_micro"), profit_share_bps=int(r.get("profit_share_bps") or 0),
            versions=[version_out(ver, warning=None) for ver in svc.store.list_versions(conn, str(r["id"]))[:5]])
            for r in rows]
    return S.Page[S.AdminStrategyOut](items=items, next_cursor=nxt)


@router.post("/strategies/{strategy_id}/list", response_model=S.AdminActionOut)
def list_strategy(strategy_id: UUID, body: S.StrategyListIn, ctx: AuthCtx = Depends(admin_step_up),
                  svc: Services = Depends(get_services)) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        st = svc.store.get_strategy(conn, str(strategy_id))
        ver = svc.store.get_version(conn, str(body.version_id))
        if st is None or ver is None or str(ver["strategy_id"]) != str(strategy_id):
            raise NotFound("strategy or version not found")
        if st["status"] == "delisted":
            raise Conflict("delisted strategies cannot be relisted")
        _require_trusted_or_conflict(conn, svc, list(ver.get("markets") or st["markets"] or []))
        return _propose(conn, svc, ctx, kind="strategy_list", target=f"strategy:{strategy_id}",
                        payload={"version_id": str(body.version_id), "version": ver["version"],
                                 "terms": _strategy_snapshot(st)}, reason=body.reason)


def _set_status_now(strategy_id: UUID, status: str, body: S.DecisionIn, ctx: AuthCtx, svc: Services,
                    allowed_from: tuple[str, ...]) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        st = svc.store.get_strategy(conn, str(strategy_id), for_update=True)
        if st is None:
            raise NotFound("strategy not found")
        if st["status"] not in allowed_from:
            raise Conflict("not allowed from this status", status=st["status"])
        svc.store.set_strategy_status(conn, str(strategy_id), status)
        # F6: proposals reviewed against the previous state can never be approved later
        cancelled = svc.store.cancel_pending_changes(conn, f"strategy:{strategy_id}",
                                                     f"superseded: strategy {status} by admin", svc.now())
        ended = {"closing": 0, "cancelled": 0}
        if status == "delisted":
            # H5: subscriptions END (closing → reduce-only exit; paused → cancelled); billing stops; users alerted
            ended = billing_ops.end_strategy_subscriptions(conn, svc, strategy_id=str(strategy_id),
                                                           strategy_name=st.get("name"))
        svc.notifier.notify(conn, user_id=None, severity="warn", kind=f"strategy_{status}",
                            payload={"strategy_id": str(strategy_id), "subscriptions_closing": ended["closing"],
                                     "subscriptions_cancelled": ended["cancelled"],
                                     "changes_cancelled": len(cancelled)})
        svc.audit.write(conn, actor=ctx.actor, action=f"strategy.{status}", target=f"strategy:{strategy_id}",
                        payload={"reason": body.reason, "from": st["status"], **{f"subscriptions_{k}": n
                                                                                   for k, n in ended.items()},
                                 "changes_cancelled": [str(c["id"]) for c in cancelled]},
                        ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied")


@router.post("/strategies/{strategy_id}/pause", response_model=S.AdminActionOut)
def pause_strategy(strategy_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                   svc: Services = Depends(get_services)) -> S.AdminActionOut:
    return _set_status_now(strategy_id, "paused", body, ctx, svc, ("listed",))


@router.post("/strategies/{strategy_id}/delist", response_model=S.AdminActionOut)
def delist_strategy(strategy_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                    svc: Services = Depends(get_services)) -> S.AdminActionOut:
    return _set_status_now(strategy_id, "delisted", body, ctx, svc, ("listed", "paused", "review", "draft"))


@router.post("/strategies/{strategy_id}/reject", response_model=S.AdminActionOut)
def reject_strategy(strategy_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                    svc: Services = Depends(get_services)) -> S.AdminActionOut:
    return _set_status_now(strategy_id, "draft", body, ctx, svc, ("review",))


@router.post("/strategies/{strategy_id}/price", response_model=S.AdminActionOut)
def set_price(strategy_id: UUID, body: S.PriceSetIn, ctx: AuthCtx = Depends(admin_step_up),
              svc: Services = Depends(get_services)) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        st = svc.store.get_strategy(conn, str(strategy_id))
        if st is None:
            raise NotFound("strategy not found")
        if not st["in_house"]:
            raise Forbidden("creators set their own prices")
        return _propose(conn, svc, ctx, kind="strategy_price", target=f"strategy:{strategy_id}",
                        payload={"price_monthly_micro": int(body.price_monthly_micro),
                                 "previous_micro": st.get("price_monthly_micro")}, reason=body.reason)


# ============================================================================================ users
@router.get("/users", response_model=S.Page[S.AdminUserOut])
def search_users(q: Optional[str] = Query(None, min_length=3, max_length=120),
                 limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                 ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> S.Page[S.AdminUserOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.search_users(conn, q, limit, cur), limit)
        # F12: PII reads are audited (the query is stored hashed, not in clear)
        svc.audit.write(conn, actor=ctx.actor, action="admin.read.users", target="",
                        payload={"q_sha256": deps_hash(q) if q else None, "n": len(rows)}, ip_hash=ctx.ip_hash)
    return S.Page[S.AdminUserOut](items=[S.AdminUserOut(**{k: r.get(k) for k in S.AdminUserOut.model_fields})
                                         for r in rows], next_cursor=nxt)


@router.post("/users/{user_id}/suspend", response_model=S.AdminActionOut)
def suspend_user(user_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                 svc: Services = Depends(get_services)) -> S.AdminActionOut:
    if str(user_id) == ctx.user_id:
        raise Forbidden("you cannot suspend yourself")
    with svc.db.begin() as conn:
        target = svc.store.get_user(conn, str(user_id))
        if target is None:
            raise NotFound("user not found")
        if target.get("role") == "admin":
            # F12: one admin must not lock the other out (maker-checker deadlock) → a second admin approves
            return _propose(conn, svc, ctx, kind="user_suspend", target=f"user:{user_id}", payload={},
                            reason=body.reason)
        if not svc.store.set_user_status(conn, str(user_id), "suspended"):
            raise NotFound("user not found")
        svc.audit.write(conn, actor=ctx.actor, action="user.suspend", target=f"user:{user_id}",
                        payload={"reason": body.reason}, ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied")


@router.post("/users/{user_id}/unsuspend", response_model=S.AdminActionOut)
def unsuspend_user(user_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                   svc: Services = Depends(get_services)) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        user = svc.store.get_user(conn, str(user_id))
        if user is None:
            raise NotFound("user not found")
        if user["status"] != "suspended":
            raise Conflict("user is not suspended")
        return _propose(conn, svc, ctx, kind="user_unsuspend", target=f"user:{user_id}", payload={},
                        reason=body.reason)


@router.post("/users/{user_id}/kyc", response_model=S.AdminActionOut)
def kyc_decision(user_id: UUID, body: S.KycDecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                 svc: Services = Depends(get_services)) -> S.AdminActionOut:
    """Creator KYC verdict by ONE admin (owner decision; documents stay at the provider). Approval unlocks listing,
    paid posts and payouts (those keep their own two-admin rules). Manual provider: pending/rejected → approved.
    Sumsub: only a provider GREEN (``provider_approved``) can be confirmed. Rejection: immediate."""
    uid = str(user_id)
    with svc.db.begin() as conn:
        kyc = svc.store.get_kyc(conn, uid)
        if kyc is None:
            raise NotFound("no KYC session for this user")
        if body.decision == "rejected":
            if kyc["status"] == "rejected":
                raise Conflict("KYC already rejected")
            svc.store.set_kyc_status(conn, uid, "rejected")
            svc.notifier.notify(conn, user_id=uid, severity="info", kind="kyc_status", payload={"status": "rejected"})
            svc.audit.write(conn, actor=ctx.actor, action="kyc.reject", target=f"user:{uid}",
                            payload={"reason": body.reason, "provider": kyc["provider"],
                                     "provider_ref": kyc["provider_ref"], "from": kyc["status"]}, ip_hash=ctx.ip_hash)
            return S.AdminActionOut(status="applied")
        if uid == ctx.user_id:
            raise Forbidden("an admin cannot approve their own KYC")
        if kyc["status"] == "approved":
            raise Conflict("KYC already approved")
        if kyc["provider"] != "manual" and kyc["status"] != "provider_approved":
            raise Conflict("the KYC provider has not approved this applicant yet", reason="provider_not_approved",
                           status=kyc["status"])
        if not svc.store.set_kyc_status(conn, uid, "approved"):
            raise Conflict("KYC state changed; reload")
        svc.notifier.notify(conn, user_id=uid, severity="info", kind="kyc_status", payload={"status": "approved"})
        svc.audit.write(conn, actor=ctx.actor, action="kyc.approve", target=f"user:{uid}",
                        payload={"reason": body.reason, "provider": kyc["provider"],
                                 "provider_ref": kyc["provider_ref"], "from": kyc["status"]}, ip_hash=ctx.ip_hash)
    return S.AdminActionOut(status="applied")


# ============================================================================================ alerts / reconciliation
@router.get("/alerts", response_model=S.Page[S.AlertOut])
def admin_alerts(severity: Optional[Literal["info", "warn", "critical"]] = Query(None),
                 unacked: bool = Query(True), ops_only: bool = Query(False),
                 limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                 ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> S.Page[S.AlertOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.admin_list_alerts(conn, severity=severity, unacked_only=unacked,
                                                            ops_only=ops_only, limit=limit, cursor=cur), limit)
        svc.audit.write(conn, actor=ctx.actor, action="admin.read.alerts", target="",
                        payload={"severity": severity, "ops_only": ops_only, "n": len(rows)}, ip_hash=ctx.ip_hash)
    return S.Page[S.AlertOut](items=[alert_out(r) for r in rows], next_cursor=nxt)


@router.post("/alerts/{alert_id}/ack", response_model=S.Ok)
def admin_ack(alert_id: UUID, ctx: AuthCtx = Depends(admin_step_up), svc: Services = Depends(get_services)) -> S.Ok:
    with svc.db.begin() as conn:
        if not svc.store.admin_ack_alert(conn, str(alert_id), ctx.user_id, svc.now()):
            raise NotFound("alert not found or already acknowledged")
        svc.audit.write(conn, actor=ctx.actor, action="alert.ack", target=f"alert:{alert_id}", payload={},
                        ip_hash=ctx.ip_hash)
    return S.Ok()


@router.get("/reconciliation", response_model=S.ReconciliationOut)
def reconciliation(ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> S.ReconciliationOut:
    with svc.db.begin() as conn:
        report = svc.jobs.latest_reconciliation(conn)
    generated = None
    if report and isinstance(report.get("generated_at"), str):
        from app.api.validation import InputError, parse_issued_at
        try:
            generated = parse_issued_at(report["generated_at"])
        except InputError:
            generated = None
    return S.ReconciliationOut(report=report, generated_at=generated)


# ============================================================================================ held USDC deposits
# suspense:usdc_unattributed release, maker-checker (app/api/suspense.py; RUNBOOK §13.3). Every POST: step-up +
# Idempotency-Key (replays return the first response) + audit log; the proposing admin can never decide.
def _release_out(r: dict) -> S.SuspenseReleaseOut:
    from app.api import suspense
    return S.SuspenseReleaseOut(**suspense.release_out(r))


@router.get("/held-deposits", response_model=S.HeldDepositsOut)
def list_held_deposits(open: bool = Query(True), limit: int = Query(50, ge=1, le=100),
                       cursor: Optional[str] = Query(None, max_length=200), ctx: AuthCtx = Depends(admin_user),
                       svc: Services = Depends(get_services)) -> S.HeldDepositsOut:
    from app.api import suspense
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(suspense.list_held(conn, svc, open_only=open, limit=limit, cursor=cur), limit)
        balance = svc.store.suspense_balance(conn)
    return S.HeldDepositsOut(items=[S.HeldDepositOut(**suspense.held_out(r)) for r in rows], next_cursor=nxt,
                             suspense_balance_micro=balance)


@router.get("/held-deposits/releases", response_model=S.Page[S.SuspenseReleaseOut])
def list_suspense_releases(status: Optional[Literal["proposed", "approved", "sent", "rejected"]] = Query(None),
                           limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                           ctx: AuthCtx = Depends(admin_user),
                           svc: Services = Depends(get_services)) -> S.Page[S.SuspenseReleaseOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_suspense_releases(conn, status, limit, cur), limit)
    return S.Page[S.SuspenseReleaseOut](items=[_release_out(r) for r in rows], next_cursor=nxt)


@router.post("/held-deposits/{tx_hash}/release", response_model=S.SuspenseReleaseOut, status_code=201,
             dependencies=[user_limit("admin_suspense", 30, 3600)])
def propose_suspense_release(body: S.SuspenseReleaseIn, tx_hash: str = Path(..., pattern=r"^0x[0-9a-fA-F]{64}$"),
                             ctx: AuthCtx = Depends(admin_step_up), key: str = Depends(idempotency_key),
                             svc: Services = Depends(get_services)):
    """Maker (admin A): attribute the held transfer to the user whose VERIFIED wallet sent it, or refund it to the
    sender. Transfers held before the sender was recorded: ``sender_address`` is verified on-chain here first."""
    from app.api import suspense
    h = suspense.norm_hash(tx_hash)
    verified: Optional[str] = None
    with svc.db.begin() as conn:
        held = suspense.held_or_404(conn, svc, h)
    if held.get("sender_address") is None and body.sender_address:
        treasury = (svc.settings.treasury_address or "").lower()
        if treasury and svc.hl.find_usd_send(sender=body.sender_address, destination=treasury,
                                             amount_micro=int(held["amount_micro"]), tx_hash=h):
            verified = body.sender_address.lower()

    def work(conn: Any) -> S.SuspenseReleaseOut:
        return _release_out(suspense.propose(
            conn, svc, ctx, tx_hash=h, action=body.action, user_id=str(body.user_id) if body.user_id else None,
            sender_address=body.sender_address, evidence=body.evidence, onchain_verified_sender=verified))

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope=f"POST /admin/held-deposits/{h}/release",
                          payload=body, work=work, status_code=201)


@router.post("/held-deposits/releases/{release_id}/approve", response_model=S.SuspenseReleaseOut,
             dependencies=[user_limit("admin_suspense", 30, 3600)])
def approve_suspense_release(release_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                             key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    """Checker (admin B ≠ A): ONE ledger transaction out of suspense (key suspense_release:{hash})."""
    from app.api import suspense

    def work(conn: Any) -> S.SuspenseReleaseOut:
        return _release_out(suspense.approve(conn, svc, ctx, release_id=str(release_id), reason=body.reason))

    return run_idempotent(svc, user_id=ctx.user_id, key=key,
                          scope=f"POST /admin/held-deposits/releases/{release_id}/approve", payload=body, work=work)


@router.post("/held-deposits/releases/{release_id}/reject", response_model=S.SuspenseReleaseOut,
             dependencies=[user_limit("admin_suspense", 30, 3600)])
def reject_suspense_release(release_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                            key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    from app.api import suspense

    def work(conn: Any) -> S.SuspenseReleaseOut:
        return _release_out(suspense.reject(conn, svc, ctx, release_id=str(release_id), reason=body.reason))

    return run_idempotent(svc, user_id=ctx.user_id, key=key,
                          scope=f"POST /admin/held-deposits/releases/{release_id}/reject", payload=body, work=work)


@router.post("/held-deposits/releases/{release_id}/typed-data", response_model=S.SuspenseRefundTypedDataOut)
def suspense_refund_typed_data(release_id: UUID, body: S.PayoutTypedDataIn, ctx: AuthCtx = Depends(admin_step_up),
                               svc: Services = Depends(get_services)) -> S.SuspenseRefundTypedDataOut:
    """usdSend typed data for an APPROVED refund (destination = the recorded on-chain sender; hardware wallet)."""
    from app.api import suspense
    with svc.db.begin() as conn:
        rel, payload = suspense.refund_typed_data(conn, svc, ctx, release_id=str(release_id),
                                                  signature_chain_id=body.signature_chain_id)
    return S.SuspenseRefundTypedDataOut(release=_release_out(rel), payload=payload,
                                        exchange_url=svc.settings.hl_api_url.rstrip("/") + "/exchange")


@router.post("/held-deposits/releases/{release_id}/sent", response_model=S.SuspenseReleaseOut,
             dependencies=[user_limit("admin_suspense", 30, 3600)])
def suspense_refund_sent(release_id: UUID, body: S.PayoutSentIn, ctx: AuthCtx = Depends(admin_step_up),
                         key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    """Record the refund usdSend: verified on-chain (treasury → sender, this hash, exact amount) before posting."""
    from app.api import suspense
    tx_hash = body.tx_hash.lower()
    with svc.db.begin() as conn:
        rel = suspense.check_refund_sendable(conn, svc, release_id=str(release_id), refund_tx_hash=tx_hash)
    if not svc.hl.find_usd_send(sender=svc.settings.treasury_address, destination=str(rel["sender_address"]),
                                amount_micro=int(rel["amount_micro"]), tx_hash=tx_hash):
        raise ValidationFailed("transfer not found on Hyperliquid for this amount and destination (yet)")

    def work(conn: Any) -> S.SuspenseReleaseOut:
        return _release_out(suspense.record_refund_sent(conn, svc, ctx, release_id=str(release_id),
                                                        refund_tx_hash=tx_hash, time_ms=body.time_ms))

    return run_idempotent(svc, user_id=ctx.user_id, key=key,
                          scope=f"POST /admin/held-deposits/releases/{release_id}/sent", payload=body, work=work)


# ============================================================================================ trusted builder dexes
# SPEC §12 (owner, 30 Sep 2026) / REVIEW_TRADING_KEYS F1. SQL + policy: app/strategies/dexes.py (shared with the
# executor's pre-trade guard and the signal ingest, which re-read the table every tick / run).
def _dex_out(r: dict) -> S.TrustedDexOut:
    return S.TrustedDexOut(dex=r["dex"], active=r["removed_at"] is None, added_by=r["added_by"], reason=r["reason"],
                           created_at=r["created_at"], removed_at=r.get("removed_at"), removed_by=r.get("removed_by"),
                           removal_reason=r.get("removal_reason"))


@router.get("/dexes", response_model=list[S.TrustedDexOut])
def list_dexes(ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> list[S.TrustedDexOut]:
    with svc.db.begin() as conn:
        return [_dex_out(r) for r in svc.store.list_trusted_dexes(conn)]


@router.post("/dexes", response_model=S.TrustedDexOut, status_code=201,
             dependencies=[user_limit("admin_dexes", 20, 3600)])
def add_dex(body: S.TrustedDexIn, ctx: AuthCtx = Depends(admin_step_up),
            svc: Services = Depends(get_services)) -> S.TrustedDexOut:
    """ONE admin approves a builder dex (owner decision): strategies may then list and trade its markets."""
    from app.strategies.dexes import normalize_dex
    dex = normalize_dex(body.dex)
    with svc.db.begin() as conn:
        row = svc.store.add_trusted_dex(conn, dex, by=ctx.actor, reason=body.reason)
        if row is None:
            raise Conflict("this dex is already trusted", dex=dex)
        svc.notifier.notify(conn, user_id=None, severity="warn", kind="trusted_dex_added",
                            payload={"dex": dex, "by": ctx.user_id, "reason": body.reason})
        svc.audit.write(conn, actor=ctx.actor, action="dex.trust.add", target=f"dex:{dex}",
                        payload={"reason": body.reason}, ip_hash=ctx.ip_hash)
    return _dex_out(row)


@router.post("/dexes/{dex}/remove", response_model=S.TrustedDexRemoveOut,
             dependencies=[user_limit("admin_dexes", 20, 3600)])
def remove_dex(body: S.DecisionIn, dex: str = Path(..., max_length=20), ctx: AuthCtx = Depends(admin_step_up),
               svc: Services = Depends(get_services)) -> S.TrustedDexRemoveOut:
    """Protective, immediate, one admin: new entries on this dex's markets stop from the next executor tick (exits
    continue); creators can no longer create, upload, or list strategies on it."""
    from app.strategies.dexes import normalize_dex
    if dex == "" or dex.strip() == "":
        raise ValidationFailed("the validator dex cannot be removed")
    d = normalize_dex(dex)
    with svc.db.begin() as conn:
        row = svc.store.remove_trusted_dex(conn, d, by=ctx.actor, reason=body.reason)
        if row is None:
            raise NotFound("no active trusted dex with this name", dex=d)
        affected = svc.store.strategies_on_dex(conn, d)
        markets = sorted({m for st in affected for m in (st.get("markets") or []) if m.split(":", 1)[0] == d})
        svc.notifier.notify(conn, user_id=None, severity="critical", kind="trusted_dex_removed",
                            payload={"dex": d, "by": ctx.user_id, "reason": body.reason,
                                     "strategies": [st["slug"] for st in affected][:50], "markets": markets[:50]})
        svc.audit.write(conn, actor=ctx.actor, action="dex.trust.remove", target=f"dex:{d}",
                        payload={"reason": body.reason, "strategies": [str(st["id"]) for st in affected][:100],
                                 "markets_entries_paused": markets[:100]}, ip_hash=ctx.ip_hash)
    return S.TrustedDexRemoveOut(dex=_dex_out(row), affected_strategies=[st["slug"] for st in affected],
                                 markets_entries_paused=markets)
