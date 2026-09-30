"""/me: profile, first-touch referral binding, platform plan changes.

GET/PATCH /me need only a valid MFA session (no consent gate) so the web app can load the profile and bind the
referral captured before sign-in. Plan changes move money → consent gate + Idempotency-Key.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    consented_user,
    current_user,
    get_services,
    idempotency_key,
    missing_consents,
    run_idempotent,
    user_limit,
)
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

router = APIRouter(tags=["me"])

REFERRAL_WINDOW = timedelta(days=30)


def _me_out(conn, svc: Services, user: dict) -> S.MeOut:
    uid = str(user["id"])
    return S.MeOut(
        id=user["id"], email=user.get("email"), display_name=user.get("display_name"), role=user["role"],
        plan=user["plan"], status=user["status"], referral_code=user.get("referral_code") or "",
        country_attested=user.get("country_attested"), mfa_enrolled=bool(user.get("mfa_enrolled")),
        created_at=user["created_at"], consents_complete=not missing_consents(conn, svc, uid),
        wallets=[S.WalletOut(**w) for w in svc.store.list_wallets(conn, uid)],
        kyc_status=(svc.store.get_kyc(conn, uid) or {}).get("status"),
    )


@router.get("/me", response_model=S.MeOut)
def get_me(ctx: AuthCtx = Depends(current_user), svc: Services = Depends(get_services)) -> S.MeOut:
    with svc.db.begin() as conn:
        user = svc.store.get_user(conn, ctx.user_id)
        return _me_out(conn, svc, user)


@router.patch("/me", response_model=S.MeOut, dependencies=[user_limit("me_patch", 10, 60)])
def patch_me(body: S.MePatchIn, ctx: AuthCtx = Depends(current_user),
             svc: Services = Depends(get_services)) -> S.MeOut:
    with svc.db.begin() as conn:
        user = svc.store.get_user(conn, ctx.user_id, for_update=True)
        if body.display_name is not None:
            svc.store.update_display_name(conn, ctx.user_id, body.display_name)
        if body.referral_code_used is not None:
            _bind_referral(conn, svc, ctx, user, body.referral_code_used)
        user = svc.store.get_user(conn, ctx.user_id)
        return _me_out(conn, svc, user)


def _bind_referral(conn, svc: Services, ctx: AuthCtx, user: dict, raw_code: str) -> None:
    """SPEC §1.2: first-touch, once, immutable, within 30 days of sign-up, no self-referral."""
    code = svc.domain.normalize_referral_code(raw_code)
    if code is None:
        raise ValidationFailed("invalid referral code")
    if user.get("referred_by"):
        raise Conflict("a referral is already bound to this account")
    if svc.now() - user["created_at"] > REFERRAL_WINDOW:
        raise Forbidden("referral codes can only be applied within 30 days of sign-up")
    referrer = svc.store.get_user_by_referral_code(conn, code)
    if referrer is None or referrer.get("status") != "active":
        raise NotFound("unknown referral code")
    rid = str(referrer["id"])
    reasons = svc.domain.self_referral_reasons(
        referrer=(rid, [w["address"] for w in svc.store.list_wallets(conn, rid)], [referrer.get("device_fp_hash")]),
        referee=(ctx.user_id, [w["address"] for w in svc.store.list_wallets(conn, ctx.user_id)],
                 [user.get("device_fp_hash")]))
    if reasons:
        svc.audit.write(conn, actor=ctx.actor, action="referral.self_referral_blocked", target=f"user:{ctx.user_id}",
                        payload={"referrer": rid, "reasons": list(reasons)}, ip_hash=ctx.ip_hash)
        raise Forbidden("self-referral is not allowed")
    if not svc.store.bind_referrer(conn, user_id=ctx.user_id, referrer_id=rid):
        raise Conflict("a referral is already bound to this account")
    svc.audit.write(conn, actor=ctx.actor, action="referral.bind", target=f"user:{ctx.user_id}",
                    payload={"referrer": rid}, ip_hash=ctx.ip_hash)


@router.post("/me/plan", response_model=S.PlanChangeOut, dependencies=[user_limit("plan", 5, 3600)])
def change_plan(body: S.PlanChangeIn, ctx: AuthCtx = Depends(consented_user),
                key: str = Depends(idempotency_key), svc: Services = Depends(get_services)):
    """Switch plan now. Paid plans charge the first month immediately from the fee balance (no proration or
    refund on switching); renewals are the settlement job's (users.plan_period_end)."""

    def work(conn) -> S.PlanChangeOut:
        user = svc.store.get_user(conn, ctx.user_id, for_update=True)
        if user["plan"] == body.plan:
            raise Conflict("already on this plan")
        live = svc.store.count_live_subscriptions(conn, ctx.user_id)
        if not svc.domain.plan_allows(body.plan, live):
            raise Conflict("too many active strategies for this plan; cancel some first", active=live)
        now = svc.now()
        charged, period_end = 0, None
        price = svc.domain.plan_price(body.plan)
        if price > 0:
            ledger_ops.require_balance(conn, svc, ctx.user_id, price)
            charged, _ = ledger_ops.charge_plan(conn, svc, user_id=ctx.user_id, plan=body.plan, period_start=now,
                                                actor=ctx.actor)
            period_end = svc.domain.add_months(now, 1)
        svc.store.set_plan(conn, ctx.user_id, body.plan, period_end)
        svc.audit.write(conn, actor=ctx.actor, action="plan.change", target=f"user:{ctx.user_id}",
                        payload={"from": user["plan"], "to": body.plan, "charged_micro": charged},
                        ip_hash=ctx.ip_hash)
        return S.PlanChangeOut(plan=body.plan, charged_micro=charged, period_end=period_end,
                               fee_balance_micro=ledger_ops.spendable(conn, svc, ctx.user_id))

    return run_idempotent(svc, user_id=ctx.user_id, key=key, scope="POST /me/plan", payload=body, work=work)
