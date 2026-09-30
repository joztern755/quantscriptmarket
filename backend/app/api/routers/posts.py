"""Posts ("subletters"): GET /posts/{id} (body only if free, purchased, own, or admin) and
POST /posts/{id}/purchase (Idempotency-Key; paid posts need a plan with the `paid_posts` feature — Pro/Max).
Price split: creator = price − $1, platform $1 (app.domain.fees.post_sale_split); in-house → platform."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    consented_user,
    get_services,
    idempotency_key,
    run_idempotent,
    user_limit,
)
from app.errors import Conflict, Forbidden, NotFound

router = APIRouter(prefix="/posts", tags=["posts"])


@router.get("/{post_id}", response_model=S.PostOut)
def get_post(post_id: UUID, ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.PostOut:
    with svc.db.begin() as conn:
        p = svc.store.get_post(conn, str(post_id))
        if p is None or (p.get("published_at") is None and str(p["creator_id"]) != ctx.user_id):
            raise NotFound("post not found")
        purchased = svc.store.has_purchased(conn, str(post_id), ctx.user_id)
    visible = int(p["price_micro"]) == 0 or purchased or str(p["creator_id"]) == ctx.user_id or ctx.role == "admin"
    return S.PostOut(id=p["id"], title=p["title"], price_micro=int(p["price_micro"]),
                     strategy_slug=p.get("strategy_slug"), published_at=p.get("published_at"),
                     body=p.get("body") if visible else None, purchased=purchased)


@router.post("/{post_id}/purchase", response_model=S.PurchaseOut, status_code=201,
             dependencies=[user_limit("post_purchase", 20, 3600)])
def purchase_post(post_id: UUID, ctx: AuthCtx = Depends(consented_user), key: str = Depends(idempotency_key),
                  svc: Services = Depends(get_services)):
    econ = svc.settings.economics

    def work(conn: Any) -> S.PurchaseOut:
        user = svc.store.get_user(conn, ctx.user_id, for_update=True)
        p = svc.store.get_post(conn, str(post_id))
        if p is None or p.get("published_at") is None:
            raise NotFound("post not found")
        if int(p["price_micro"]) == 0:
            raise Conflict("this post is free")
        if str(p["creator_id"]) == ctx.user_id:
            raise Conflict("you wrote this post")
        if "paid_posts" not in econ.plan(user["plan"]).features:
            raise Forbidden("paid posts need the Pro or Max plan", plan=user["plan"])
        if svc.store.has_purchased(conn, str(post_id), ctx.user_id):
            return S.PurchaseOut(post_id=post_id, charged_micro=0,
                                 fee_balance_micro=ledger_ops.spendable(conn, svc, ctx.user_id))
        ledger_ops.require_balance(conn, svc, ctx.user_id, int(p["price_micro"]))
        charged, tx = ledger_ops.charge_post(conn, svc, user_id=ctx.user_id, post_row=p, actor=ctx.actor)
        svc.store.insert_purchase(conn, post_id=str(post_id), user_id=ctx.user_id, price_micro=charged, tx_id=tx)
        svc.audit.write(conn, actor=ctx.actor, action="post.purchase", target=f"post:{post_id}",
                        payload={"price_micro": charged, "ledger_tx": tx}, ip_hash=ctx.ip_hash)
        return S.PurchaseOut(post_id=post_id, charged_micro=charged,
                             fee_balance_micro=ledger_ops.spendable(conn, svc, ctx.user_id))

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope=f"POST /posts/{post_id}/purchase", payload={},
                          work=work, status_code=201)
