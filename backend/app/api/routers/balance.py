"""Fee balance: GET /balance and GET /balance/ledger (the user's fee-balance ledger history)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, decode_cursor_or_422, get_services, next_cursor

router = APIRouter(prefix="/balance", tags=["balance"])


@router.get("", response_model=S.BalanceOut)
def get_balance(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.BalanceOut:
    econ = svc.settings.economics
    with svc.db.begin() as conn:
        bal = ledger_ops.spendable(conn, svc, ctx.user_id)
        pending = svc.store.pending_withdrawals_total(conn, ctx.user_id)
        prices = svc.store.live_subscription_prices(conn, ctx.user_id)
        user = svc.store.get_user(conn, ctx.user_id)
    plan_price = svc.domain.plan_price(user["plan"]) if user["plan"] != "free" else 0
    need = svc.domain.estimate_monthly_need(prices, plan_price)
    return S.BalanceOut(fee_balance_micro=bal, withdrawals_pending_micro=pending, estimated_monthly_need_micro=need,
                        reserve_required_micro=econ.min_topup_micro if prices else 0,
                        min_topup_micro=econ.min_topup_micro)


@router.get("/ledger", response_model=S.Page[S.LedgerEntryOut])
def ledger_history(limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                   ctx: AuthCtx = Depends(consented_user),
                   svc: Services = Depends(get_services)) -> S.Page[S.LedgerEntryOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.ledger_history(conn, ledger_ops.fee_balance(ctx.user_id), limit, cur), limit)
    # Liability account: a CREDIT (negative raw amount) increases the user's balance.
    return S.Page[S.LedgerEntryOut](
        items=[S.LedgerEntryOut(tx_id=r["tx_id"], kind=r["kind"], memo=r.get("memo"),
                                amount_micro=-int(r["raw_amount_micro"]), created_at=r["created_at"]) for r in rows],
        next_cursor=nxt)
