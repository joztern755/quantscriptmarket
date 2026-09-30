"""Fee-balance deposits.

Stripe: POST /deposits/stripe creates a PaymentIntent (Idempotency-Key → Stripe idempotency token; a retry
returns the same PaymentIntent). Nothing is credited here: only the signed webhook credits (webhooks.py), net
of the actual Stripe fee when fees are passed to the user (app.payments.stripe_pay).
USDC: POST /deposits/usdc/typed-data returns the usdSend typed data (destination = OUR treasury from config,
never from the client); the browser signs and posts it to Hyperliquid. POST /deposits/usdc/confirm makes NO
Hyperliquid call (REVIEW_AUTH_API F1: a user loop could otherwise download the treasury's whole ledger on our shared
IP and starve the executor): it records a scan request (deposit_scan_requests, lookback clamped server-side to
max(now − 48 h, the wallet's verification time)) and returns what the deposits-scan job (every 5 min, single-flight,
cursor) has ALREADY credited to this user since then. Credits are idempotent on the transfer hash (usdc_hl:{hash}).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    consented_user,
    decode_cursor_or_422,
    get_services,
    idempotency_key,
    next_cursor,
    run_idempotent,
    user_limit,
)
from app.errors import Forbidden, ValidationFailed

router = APIRouter(prefix="/deposits", tags=["deposits"])


def _deposit_out(r: dict) -> S.DepositOut:
    return S.DepositOut(id=r["id"], method=r["method"], amount_micro=int(r["amount_micro"]), status=r["status"],
                        external_ref=r.get("external_ref"), created_at=r["created_at"])


@router.get("", response_model=S.Page[S.DepositOut])
def list_deposits(limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                  ctx: AuthCtx = Depends(consented_user),
                  svc: Services = Depends(get_services)) -> S.Page[S.DepositOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_deposits(conn, ctx.user_id, limit, cur), limit)
    return S.Page[S.DepositOut](items=[_deposit_out(r) for r in rows], next_cursor=nxt)


@router.post("/stripe", response_model=S.StripeDepositOut, status_code=201,
             dependencies=[user_limit("deposit_stripe", 6, 60)])
def stripe_deposit(body: S.StripeDepositIn, ctx: AuthCtx = Depends(consented_user),
                   key: str = Depends(idempotency_key), svc: Services = Depends(get_services)) -> S.StripeDepositOut:
    amount = int(body.amount_micro)
    if amount < svc.settings.economics.min_topup_micro:
        raise ValidationFailed("amount below the minimum top-up", min_micro=svc.settings.economics.min_topup_micro)
    # Stripe's own idempotency makes a retried request return the same PaymentIntent; the token is derived from
    # (user, Idempotency-Key) so another user's key can never collide. client_secret is not persisted by us.
    token = hashlib.sha256(f"{ctx.user_id}:{key}".encode()).hexdigest()[:48]
    view = svc.stripe.create_topup_intent(user_id=ctx.user_id, amount_micro=amount, token=token)
    with svc.db.begin() as conn:
        svc.store.insert_pending_deposit(conn, user_id=ctx.user_id, method="stripe",
                                         external_ref=view["payment_intent_id"], amount_micro=int(view["credit_micro"]),
                                         currency=str(view.get("currency") or "usd"),
                                         amount_minor=view.get("amount_minor"))
        svc.audit.write(conn, actor=ctx.actor, action="deposit.stripe.intent",
                        target=f"stripe:{view['payment_intent_id']}",
                        payload={"amount_micro": amount, "currency": view.get("currency")}, ip_hash=ctx.ip_hash)
    return S.StripeDepositOut(payment_intent_id=view["payment_intent_id"], client_secret=view["client_secret"],
                              amount_micro=int(view["credit_micro"]))


@router.post("/usdc/typed-data", response_model=S.UsdcTypedDataOut,
             dependencies=[user_limit("deposit_usdc_td", 20, 60)])
def usdc_typed_data(body: S.UsdcTypedDataIn, ctx: AuthCtx = Depends(consented_user),
                    svc: Services = Depends(get_services)) -> S.UsdcTypedDataOut:
    s = svc.settings
    if not s.treasury_address:
        raise ValidationFailed("treasury address not configured")
    with svc.db.begin() as conn:
        if svc.store.verified_wallet(conn, ctx.user_id, body.from_address) is None:
            raise Forbidden("verify ownership of this wallet first (deposits are matched by sender)")
    time_ms = int(svc.now().timestamp() * 1000)
    req = svc.usdc.build_topup(master_address=body.from_address, amount_micro=int(body.amount_micro),
                               signature_chain_id=body.signature_chain_id, time_ms=time_ms)
    return S.UsdcTypedDataOut(from_address=body.from_address, destination=req["destination"],
                              amount_micro=int(body.amount_micro), time_ms=time_ms,
                              payload={"typed_data": req["typed_data"], "action": req["action"], "nonce": req["nonce"]},
                              exchange_url=s.hl_api_url.rstrip("/") + "/exchange")


#: server-side cap on how far back a confirm may ask the scan to look (REVIEW_AUTH_API F1)
CONFIRM_MAX_LOOKBACK = timedelta(hours=48)


@router.post("/usdc/confirm", response_model=S.UsdcConfirmOut, dependencies=[user_limit("deposit_usdc_confirm", 10, 60)])
def usdc_confirm(body: S.UsdcConfirmIn, ctx: AuthCtx = Depends(consented_user),
                 key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    now = svc.now()
    with svc.db.begin() as conn:
        wallets = {w["address"]: w["verified_at"] for w in svc.store.list_wallets(conn, ctx.user_id)
                   if w.get("verified_at")}
    if body.from_address:
        if body.from_address not in wallets:
            raise Forbidden("verify ownership of this wallet first")
        wallets = {body.from_address: wallets[body.from_address]}
    if not wallets:
        raise Forbidden("verify a wallet first")
    # lookback = max(now − 48 h, earliest verification of the wallets in question); a client time only narrows it
    since = max(now - CONFIRM_MAX_LOOKBACK, min(wallets.values()))
    if body.time_ms:
        client = datetime.fromtimestamp(body.time_ms / 1000, tz=timezone.utc) - timedelta(minutes=5)
        since = max(since, min(client, now))
    since = min(since, now)

    def work(conn: Any) -> S.UsdcConfirmOut:
        svc.store.request_deposit_scan(conn, ctx.user_id, since=since, now=now)   # wake-up hint; no HL call here
        credited = [_deposit_out(r) for r in svc.store.list_deposits(conn, ctx.user_id, 100, None)
                    if r.get("method") == "usdc_hl" and r.get("status") == "credited" and r["created_at"] >= since]
        return S.UsdcConfirmOut(credited=credited, fee_balance_micro=ledger_ops.spendable(conn, svc, ctx.user_id))

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope="POST /deposits/usdc/confirm", payload=body,
                          work=work)
