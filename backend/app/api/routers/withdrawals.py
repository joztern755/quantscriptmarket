"""Money out (maker-checker; SPEC §2.1, §5.6): POST /withdrawals (fee balance → USDC), POST /payouts (creator /
referrer earnings), GET /withdrawals (both kinds, own). Step-up + Idempotency-Key; refused while the launch phase
has payouts disabled.

A request immediately HOLDS the amount in the ledger (fee_balance/payable → withdrawals:pending/payouts:pending),
so it cannot be spent twice; two different admins approve; an admin signs the usdSend in the browser with a
hardware wallet (treasury key never on a server) and records the tx hash, which is verified on-chain before the
hold settles against treasury:hl_usdc (admin router). Rejection releases the hold.
Destination = one of the user's verified wallets (binding a wallet is itself a step-up action).
Card-funded credits are spend-only: a fee-balance withdrawal is limited to USDC-funded (withdrawable) credits.
"""
from __future__ import annotations

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
    require_payouts_enabled,
    run_idempotent,
    step_up_user,
    user_limit,
)
from app.errors import Forbidden, InsufficientBalance, ValidationFailed

router = APIRouter(tags=["withdrawals"])


def payout_out(r: dict) -> S.PayoutOut:
    return S.PayoutOut(id=r["id"], kind=r["kind"], amount_micro=int(r["amount_micro"]), to_address=r["to_address"],
                       status=r["status"], tx_hash=r.get("tx_hash"), created_at=r["created_at"])


@router.get("/withdrawals", response_model=S.Page[S.PayoutOut])
def list_withdrawals(limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                     ctx: AuthCtx = Depends(consented_user),
                     svc: Services = Depends(get_services)) -> S.Page[S.PayoutOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_user_payouts(conn, ctx.user_id, limit, cur), limit)
    return S.Page[S.PayoutOut](items=[payout_out(r) for r in rows], next_cursor=nxt)


@router.post("/withdrawals", response_model=S.PayoutOut, status_code=201,
             dependencies=[user_limit("withdraw", 5, 3600)])
def request_withdrawal(body: S.WithdrawalIn, ctx: AuthCtx = Depends(step_up_user),
                       key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    require_payouts_enabled(svc)
    amount = int(body.amount_micro)
    if amount < svc.settings.economics.min_topup_micro:
        raise ValidationFailed("amount below the minimum withdrawal", min_micro=svc.settings.economics.min_topup_micro)

    def work(conn: Any) -> S.PayoutOut:
        svc.store.lock_user(conn, ctx.user_id)
        if svc.store.verified_wallet(conn, ctx.user_id, body.to_address) is None:
            raise Forbidden("withdrawals go only to one of your verified wallets")
        ledger_ops.require_balance(conn, svc, ctx.user_id, amount)
        withdrawable = svc.store.withdrawable_usdc(conn, ctx.user_id)
        if amount > withdrawable:
            raise InsufficientBalance("card-funded balance can be spent but not withdrawn",
                                      withdrawable_micro=withdrawable)
        row = svc.store.insert_withdrawal(conn, user_id=ctx.user_id, amount_micro=amount, to_address=body.to_address)
        tx = ledger_ops.hold_withdrawal(conn, svc, user_id=ctx.user_id, withdrawal_id=str(row["id"]), amount=amount,
                                        actor=ctx.actor)
        svc.notifier.notify(conn, user_id=None, severity="warn", kind="withdrawal_requested",
                            payload={"withdrawal_id": str(row["id"]), "amount_micro": amount})
        # SPEC §12 mandatory user alert (Telegram + email via the delivery worker): "if this was not you…"
        svc.notifier.notify(conn, user_id=ctx.user_id, severity="warn", kind="withdrawal_requested",
                            payload={"amount_micro": amount, "request_id": str(row["id"])[:8]})
        svc.audit.write(conn, actor=ctx.actor, action="withdrawal.request", target=f"withdrawal:{row['id']}",
                        payload={"amount_micro": amount, "to": body.to_address, "hold_tx": tx}, ip_hash=ctx.ip_hash)
        return payout_out(row)

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope="POST /withdrawals", payload=body, work=work,
                          status_code=201)


@router.post("/payouts", response_model=S.PayoutOut, status_code=201, dependencies=[user_limit("payout", 5, 3600)])
def request_payout(body: S.PayoutRequestIn, ctx: AuthCtx = Depends(step_up_user),
                   key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    require_payouts_enabled(svc)
    amount = int(body.amount_micro)
    if amount < svc.settings.economics.min_topup_micro:
        raise ValidationFailed("amount below the minimum payout", min_micro=svc.settings.economics.min_topup_micro)
    account = (ledger_ops.creator_payable(ctx.user_id) if body.source == "creator"
               else ledger_ops.referrer_payable(ctx.user_id))

    def work(conn: Any) -> S.PayoutOut:
        svc.store.lock_user(conn, ctx.user_id)
        if body.source == "creator":
            kyc = svc.store.get_kyc(conn, ctx.user_id)
            if not kyc or kyc["status"] != "approved":
                raise Forbidden("complete creator KYC before requesting a payout", reason="kyc_required")
        if svc.store.verified_wallet(conn, ctx.user_id, body.to_address) is None:
            raise Forbidden("payouts go only to one of your verified wallets")
        available = -svc.ledger.balance(conn, account)
        if amount > available:
            raise InsufficientBalance("amount exceeds your available earnings", available_micro=available)
        account_id = svc.store.account_id(conn, account)
        if account_id is None:
            raise InsufficientBalance("no earnings yet", available_micro=0)
        row = svc.store.insert_payout(conn, user_id=ctx.user_id, ledger_account_id=account_id, amount_micro=amount,
                                      to_address=body.to_address)
        tx = ledger_ops.hold_payout(conn, svc, source_account=account, payout_id=str(row["id"]), amount=amount,
                                    actor=ctx.actor)
        svc.notifier.notify(conn, user_id=None, severity="warn", kind="payout_requested",
                            payload={"payout_id": str(row["id"]), "amount_micro": amount, "source": body.source})
        svc.audit.write(conn, actor=ctx.actor, action="payout.request", target=f"payout:{row['id']}",
                        payload={"amount_micro": amount, "source": body.source, "to": body.to_address, "hold_tx": tx},
                        ip_hash=ctx.ip_hash)
        return payout_out(row)

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope="POST /payouts", payload=body, work=work,
                          status_code=201)
