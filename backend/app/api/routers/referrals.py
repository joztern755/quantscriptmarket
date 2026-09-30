"""GET /referrals — code, share link, tier (the one used for payouts: users.referral_tier, re-evaluated daily by
the referral-tiers job), live 30-day stats towards the next tier, and earnings (referrer:{id}:payable)."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, get_services

router = APIRouter(tags=["referrals"])


@router.get("/referrals", response_model=S.ReferralsOut)
def referrals(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.ReferralsOut:
    econ = svc.settings.economics
    account = f"referrer:{ctx.user_id}:payable"
    with svc.db.begin() as conn:
        user = svc.store.get_user(conn, ctx.user_id)
        stats = svc.store.referral_stats(conn, ctx.user_id, svc.now() - timedelta(days=30))
        payable = -svc.ledger.balance(conn, account)
        total = svc.store.total_credited(conn, account)
    tiers = sorted(econ.referral_tiers, key=lambda t: t.share_of_pool_bps)
    current = next((t for t in tiers if t.name == user.get("referral_tier")), None) or \
        svc.domain.evaluate_tier(int(stats["active"]), int(stats["notional"]))
    higher = [t for t in tiers if t.share_of_pool_bps > current.share_of_pool_bps]
    nxt = higher[0] if higher else None
    return S.ReferralsOut(
        code=user.get("referral_code") or "", link=f"{svc.settings.web_origin}/?ref={user.get('referral_code') or ''}",
        tier=current.name, share_of_pool_bps=current.share_of_pool_bps,
        active_referred_users_30d=int(stats["active"]), referred_notional_30d_micro=int(stats["notional"]),
        referred_users_total=int(stats["total"]), earnings_payable_micro=payable, earnings_total_micro=total,
        next_tier=None if nxt is None else S.ReferralTierOut(name=nxt.name, min_active_users=nxt.min_active_users,
                                                             min_notional_30d_micro=nxt.min_notional_30d_micro,
                                                             share_of_pool_bps=nxt.share_of_pool_bps))
