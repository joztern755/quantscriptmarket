"""Wallet ownership proof (EIP-4361 / SIWE-style personal_sign).

POST /wallets/nonce  → single-use nonce (10 min). POST /wallets/verify {address, message, signature}: the server
parses the message and checks domain (= web origin host), URI (= web origin), address, nonce (this user's,
unused, unexpired — consumed atomically), Issued At freshness (≤ 10 min, ≤ 60 s in the future) and optional
Expiration Time; then recovers the signer and binds the wallet to the user. A verified wallet is also the only
allowed withdrawal/payout destination, so binding one is a step-up action (SPEC §5.2 "change payout address").
"""
from __future__ import annotations

import secrets
import string
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api import validation as v
from app.api.deps import AuthCtx, Services, consented_user, get_services, step_up_user, user_limit
from app.errors import Conflict, ValidationFailed

router = APIRouter(prefix="/wallets", tags=["wallets"])

NONCE_TTL = timedelta(minutes=10)
MAX_MESSAGE_AGE = timedelta(minutes=10)
_ALNUM = string.ascii_letters + string.digits


@router.post("/nonce", response_model=S.WalletNonceOut, dependencies=[user_limit("wallet_nonce", 20, 3600)])
def new_nonce(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.WalletNonceOut:
    nonce = "".join(secrets.choice(_ALNUM) for _ in range(24))
    expires = svc.now() + NONCE_TTL
    with svc.db.begin() as conn:
        svc.store.create_wallet_nonce(conn, user_id=ctx.user_id, nonce=nonce, expires_at=expires)
    return S.WalletNonceOut(nonce=nonce, expires_at=expires)


@router.post("/verify", response_model=S.WalletOut, dependencies=[user_limit("wallet_verify", 10, 3600)])
def verify_wallet(body: S.WalletVerifyIn, ctx: AuthCtx = Depends(step_up_user),
                  svc: Services = Depends(get_services)) -> S.WalletOut:
    web = urlsplit(svc.settings.web_origin)
    try:
        msg = v.parse_siwe(body.message)
        issued = v.parse_issued_at(msg["Issued At"])
        expires = v.parse_issued_at(msg["Expiration Time"]) if "Expiration Time" in msg else None
        not_before = v.parse_issued_at(msg["Not Before"]) if "Not Before" in msg else None
    except v.InputError as e:
        raise ValidationFailed(str(e)) from None
    now = svc.now()
    if msg["domain"] != web.netloc:
        raise ValidationFailed("sign-in message is for a different domain")
    uri = urlsplit(msg["URI"])
    if (uri.scheme, uri.netloc) != (web.scheme, web.netloc):
        raise ValidationFailed("sign-in message URI does not match this site")
    if msg["address"].lower() != body.address:
        raise ValidationFailed("sign-in message address does not match")
    if not (now - MAX_MESSAGE_AGE <= issued <= now + timedelta(seconds=60)):
        raise ValidationFailed("sign-in message expired; request a new nonce")
    if expires is not None and expires <= now:
        raise ValidationFailed("sign-in message expired; request a new nonce")
    if not_before is not None and not_before > now:     # F18: EIP-4361 Not Before is honoured
        raise ValidationFailed("sign-in message is not valid yet")
    signer = svc.wallet_sig.recover(body.message, body.signature)
    if signer != body.address:
        raise ValidationFailed("signature does not match the wallet address")
    with svc.db.begin() as conn:
        if not svc.store.consume_wallet_nonce(conn, user_id=ctx.user_id, nonce=msg["Nonce"], now=now):
            raise ValidationFailed("nonce is invalid, expired or already used")
        row = svc.store.upsert_verified_wallet(conn, ctx.user_id, body.address, now)
        if str(row["user_id"]) != ctx.user_id:
            svc.audit.write(conn, actor=ctx.actor, action="wallet.verify_conflict", target=f"wallet:{body.address}",
                            payload={}, ip_hash=ctx.ip_hash)
            raise Conflict("this wallet is linked to another account")
        referrer = ctx.user.get("referred_by")
        if referrer:
            # Referral binding is immutable (SPEC §1.2): a match (wallet, device, network) FLAGS the referee — no
            # referral reward from this account until ops clears it — instead of silently rewriting the binding.
            from app.api.referral_guard import flag_self_referral, self_referral_check

            ref_user = svc.store.get_user(conn, str(referrer))
            if ref_user is not None:
                _, reasons = self_referral_check(conn, svc, referrer=ref_user, referee=ctx.user,
                                                 referee_wallets=[body.address])
                if reasons:
                    flag_self_referral(conn, svc, referee_id=ctx.user_id, referrer_id=str(referrer),
                                       reasons=reasons, where="wallet_verify")
        svc.audit.write(conn, actor=ctx.actor, action="wallet.verify", target=f"wallet:{body.address}",
                        payload={"chain_id": msg["Chain ID"]}, ip_hash=ctx.ip_hash)
    return S.WalletOut(address=row["address"], verified_at=row["verified_at"])
