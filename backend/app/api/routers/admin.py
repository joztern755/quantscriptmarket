"""Admin console API. Role from OUR DB (never token claims); every mutation requires step-up and is audit-logged.

Maker-checker policy (SPEC §5.6 "all admin actions maker-checker"):
  * PROTECTIVE actions take effect immediately with one admin (speed matters in an incident): engaging a kill
    switch / pausing entries, suspending a user, pausing or delisting a strategy (its live subscriptions go
    reduce_only), rejecting a payout, rejecting a strategy back to draft.
  * PERMISSIVE actions need a second, different admin: lifting a switch (system_flags.pending_*), listing a
    strategy version, setting an in-house price, un-suspending a user (admin_changes table), approving payouts
    (withdrawals/payouts maker_admin ≠ checker_admin, enforced by DB CHECKs too; neither may be the beneficiary).
Payout execution: after approved_2 an admin requests the usdSend typed data, signs it in the browser with the
hardware treasury wallet, posts it to Hyperliquid, then records the tx hash here; we verify it on-chain before
settling the ledger hold against treasury:hl_usdc.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    admin_step_up,
    admin_user,
    decode_cursor_or_422,
    get_services,
    next_cursor,
    require_payouts_enabled,
    user_limit,
)
from app.api.routers.alerts import alert_out
from app.api.routers.creator import history_days_for, version_out
from app.api.validation import micro_to_usd_string
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[user_limit("admin", 120, 60)])

PayoutKind = Literal["withdrawal", "payout"]


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
def list_changes(status: Optional[Literal["pending", "approved", "rejected"]] = Query("pending"),
                 limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                 ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> S.Page[S.ChangeOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_changes(conn, status, limit, cur), limit)
    return S.Page[S.ChangeOut](items=[_change_out(r) for r in rows], next_cursor=nxt)


def _apply_change(conn: Any, svc: Services, ctx: AuthCtx, ch: dict) -> None:
    kind, payload = ch["kind"], ch.get("payload") or {}
    if kind == "strategy_list":
        sid, vid = ch["target"].split(":", 1)[1], str(payload["version_id"])
        st = svc.store.get_strategy(conn, sid, for_update=True)
        ver = svc.store.get_version(conn, vid)
        if st is None or ver is None or str(ver["strategy_id"]) != sid:
            raise Conflict("strategy or version no longer exists")
        if st["price_monthly_micro"] is None or st["profit_share_bps"] is None:
            raise Conflict("set a price and profit share before listing")
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
        svc.store.set_strategy_price(conn, sid, int(payload["price_monthly_micro"]))
    elif kind == "user_unsuspend":
        svc.store.set_user_status(conn, ch["target"].split(":", 1)[1], "active")
    elif kind == "kyc_approve":
        if not svc.store.set_kyc_status(conn, ch["target"].split(":", 1)[1], "approved"):
            raise Conflict("no KYC session for this user")
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
def _admin_payout_out(r: dict) -> S.AdminPayoutOut:
    return S.AdminPayoutOut(id=r["id"], kind=r["kind"], beneficiary=r["beneficiary"],
                            amount_micro=int(r["amount_micro"]), to_address=r["to_address"], status=r["status"],
                            maker_admin=r.get("maker_admin"), checker_admin=r.get("checker_admin"),
                            tx_hash=r.get("tx_hash"), created_at=r["created_at"])


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
    return S.Page[S.AdminPayoutOut](items=[_admin_payout_out(r) for r in rows], next_cursor=nxt)


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
            ok, step = svc.store.payout_approve_2(conn, kind, str(payout_id), ctx.user_id, now), "approve_2"
        else:
            raise Conflict("nothing to approve in this state", status=row["status"])
        if not ok:
            raise Conflict("state changed; reload")
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.{step}", target=f"{kind}:{payout_id}",
                        payload={"amount_micro": int(row["amount_micro"])}, ip_hash=ctx.ip_hash)
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
    return _admin_payout_out(row)


@router.post("/payouts/{kind}/{payout_id}/reject", response_model=S.AdminPayoutOut)
def reject_payout(kind: PayoutKind, payout_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                  svc: Services = Depends(get_services)) -> S.AdminPayoutOut:
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id))
        if row is None:
            raise NotFound("not found")
        if row["status"] not in ("requested", "approved_1", "approved_2"):
            raise Conflict("cannot reject in this state", status=row["status"])
        source = _source_account(conn, svc, kind, row)
        tx = ledger_ops.release_hold(conn, svc, kind=kind, row=row, source_account=source, actor=ctx.actor)
        if not svc.store.payout_reject(conn, kind, str(payout_id), ctx.user_id, body.reason):
            raise Conflict("state changed; reload")
        svc.notifier.notify(conn, user_id=str(row["beneficiary"]), severity="info", kind=f"{kind}_rejected",
                            payload={"amount_micro": int(row["amount_micro"])})
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.reject", target=f"{kind}:{payout_id}",
                        payload={"reason": body.reason, "release_tx": tx}, ip_hash=ctx.ip_hash)
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
    return _admin_payout_out(row)


@router.post("/payouts/{kind}/{payout_id}/typed-data", response_model=S.PayoutTypedDataOut)
def payout_typed_data(kind: PayoutKind, payout_id: UUID, body: S.PayoutTypedDataIn,
                      ctx: AuthCtx = Depends(admin_step_up), svc: Services = Depends(get_services)) -> S.PayoutTypedDataOut:
    require_payouts_enabled(svc)
    with svc.db.begin() as conn:
        row = svc.store.get_payout(conn, kind, str(payout_id), for_update=False)
        if row is None:
            raise NotFound("not found")
        if row["status"] != "approved_2":
            raise Conflict("needs two approvals first", status=row["status"])
        svc.audit.write(conn, actor=ctx.actor, action=f"{kind}.typed_data", target=f"{kind}:{payout_id}",
                        payload={}, ip_hash=ctx.ip_hash)
    payload = svc.typed_data.usd_send(destination=row["to_address"], amount=micro_to_usd_string(int(row["amount_micro"])),
                                      time_ms=int(svc.now().timestamp() * 1000),
                                      signature_chain_id=body.signature_chain_id)
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
        tx = ledger_ops.settle_sent(conn, svc, kind=kind, row=row, tx_hash=tx_hash, actor=ctx.actor)
        if not svc.store.payout_mark_sent(conn, kind, str(payout_id), tx_hash, tx):
            raise Conflict("state changed; reload")
        svc.notifier.notify(conn, user_id=str(row["beneficiary"]), severity="info", kind=f"{kind}_sent",
                            payload={"amount_micro": int(row["amount_micro"]), "tx_hash": tx_hash})
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
        return _propose(conn, svc, ctx, kind="strategy_list", target=f"strategy:{strategy_id}",
                        payload={"version_id": str(body.version_id), "version": ver["version"]}, reason=body.reason)


def _set_status_now(strategy_id: UUID, status: str, body: S.DecisionIn, ctx: AuthCtx, svc: Services,
                    allowed_from: tuple[str, ...]) -> S.AdminActionOut:
    with svc.db.begin() as conn:
        st = svc.store.get_strategy(conn, str(strategy_id), for_update=True)
        if st is None:
            raise NotFound("strategy not found")
        if st["status"] not in allowed_from:
            raise Conflict("not allowed from this status", status=st["status"])
        svc.store.set_strategy_status(conn, str(strategy_id), status)
        affected = 0
        if status == "delisted":
            affected = svc.store.pause_subscriptions_of_strategy(conn, str(strategy_id))
        svc.notifier.notify(conn, user_id=None, severity="warn", kind=f"strategy_{status}",
                            payload={"strategy_id": str(strategy_id), "subscriptions_reduce_only": affected})
        svc.audit.write(conn, actor=ctx.actor, action=f"strategy.{status}", target=f"strategy:{strategy_id}",
                        payload={"reason": body.reason, "from": st["status"], "subscriptions_reduce_only": affected},
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
    return S.Page[S.AdminUserOut](items=[S.AdminUserOut(**{k: r.get(k) for k in S.AdminUserOut.model_fields})
                                         for r in rows], next_cursor=nxt)


@router.post("/users/{user_id}/suspend", response_model=S.AdminActionOut)
def suspend_user(user_id: UUID, body: S.DecisionIn, ctx: AuthCtx = Depends(admin_step_up),
                 svc: Services = Depends(get_services)) -> S.AdminActionOut:
    if str(user_id) == ctx.user_id:
        raise Forbidden("you cannot suspend yourself")
    with svc.db.begin() as conn:
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
    """Record the KYC provider's verdict (documents stay at the provider). Approval unlocks listing, paid posts
    and payouts → maker-checker; rejection is protective → immediate."""
    with svc.db.begin() as conn:
        kyc = svc.store.get_kyc(conn, str(user_id))
        if kyc is None:
            raise NotFound("no KYC session for this user")
        if body.decision == "rejected":
            svc.store.set_kyc_status(conn, str(user_id), "rejected")
            svc.audit.write(conn, actor=ctx.actor, action="kyc.reject", target=f"user:{user_id}",
                            payload={"reason": body.reason, "provider_ref": kyc["provider_ref"]}, ip_hash=ctx.ip_hash)
            return S.AdminActionOut(status="applied")
        if kyc["status"] == "approved":
            raise Conflict("KYC already approved")
        return _propose(conn, svc, ctx, kind="kyc_approve", target=f"user:{user_id}",
                        payload={"provider": kyc["provider"], "provider_ref": kyc["provider_ref"]}, reason=body.reason)


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
