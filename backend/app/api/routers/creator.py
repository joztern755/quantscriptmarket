"""Creator Studio (settings.feature_creator_uploads; role creator|admin via the creator agreement consent).

Version upload (step-up, 1 MB body, ≤ 64 KB code): no-code spec → Python (app.sandbox.nocode) → STATIC validation
here (app.sandbox.validate: AST allowlist, never executes) → markets checked against live Hyperliquid meta →
walk-forward backtest in the sandbox service (no egress; this process fetches the candles and sends them) →
code sealed with the encrypt-only KMS envelope (AAD = "strategy_code:{strategy_id}:{code_hash}"; the executor
needs the same AAD to open it) → strategy_versions row (unpublished) → strategy 'review'. An admin listing
(maker-checker) publishes the version and resets the live record (live_since).
KYC is required before a creator strategy can be listed, before paid posts, and before payouts.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api import ledger_ops
from app.api import schemas as S
from app.api.deps import (
    AuthCtx,
    Services,
    creator_step_up,
    creator_user,
    get_services,
    user_limit,
)
from app.errors import Conflict, Forbidden, NotFound, ValidationFailed

router = APIRouter(prefix="/creator", tags=["creator"])

UNPROVEN_WARNING = "Backtest of a newly uploaded script can be fitted to history; not proven live yet"
EDITABLE_STATUSES = ("draft", "review")


def _finite(obj: Any) -> Any:
    """jsonb rejects NaN/Infinity: replace non-finite floats with None (recursively)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _finite(val) for k, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(val) for val in obj]
    return obj


def _strategy_out(r: dict) -> S.CreatorStrategyOut:
    return S.CreatorStrategyOut(id=r["id"], slug=r["slug"], name=r["name"], status=r["status"],
                                markets=list(r["markets"] or []), timeframe=r["timeframe"],
                                price_monthly_micro=r["price_monthly_micro"],
                                profit_share_bps=int(r["profit_share_bps"] or 0), description=r.get("description"),
                                created_at=r["created_at"])


def version_out(r: dict, *, warning: Optional[str] = UNPROVEN_WARNING) -> S.CreatorVersionOut:
    return S.CreatorVersionOut(id=r["id"], version=r["version"], code_hash=r["code_hash"],
                               published_at=r.get("published_at"), live_since=r.get("live_since"),
                               params=r.get("params") or {}, backtest=r.get("backtest"), created_at=r["created_at"],
                               warning=warning)


def _check_terms(svc: Services, profit_share_bps: Optional[int], price_micro: Optional[int]) -> None:
    cap = svc.settings.economics.profit_share_creator_cap_bps
    if profit_share_bps is not None and profit_share_bps > cap:
        raise ValidationFailed("profit share above the creator cap", cap_bps=cap)
    if price_micro is not None and price_micro < 0:
        raise ValidationFailed("price must be ≥ 0")


def _owned(conn: Any, svc: Services, ctx: AuthCtx, strategy_id: str, *, for_update: bool = False) -> dict:
    st = svc.store.get_strategy(conn, strategy_id, for_update=for_update)
    if st is None or (str(st.get("owner_user_id")) != ctx.user_id and ctx.role != "admin") or st["in_house"]:
        raise NotFound("strategy not found")
    return st


@router.get("/strategies", response_model=list[S.CreatorStrategyOut])
def my_strategies(ctx: AuthCtx = Depends(creator_user), svc: Services = Depends(get_services)) -> list[S.CreatorStrategyOut]:
    with svc.db.begin() as conn:
        return [_strategy_out(r) for r in svc.store.list_owned_strategies(conn, ctx.user_id)]


@router.post("/strategies", response_model=S.CreatorStrategyOut, status_code=201,
             dependencies=[user_limit("creator_strategy", 10, 3600)])
def create_strategy(body: S.CreatorStrategyIn, ctx: AuthCtx = Depends(creator_user),
                    svc: Services = Depends(get_services)) -> S.CreatorStrategyOut:
    _check_terms(svc, body.profit_share_bps, body.price_monthly_micro)
    unknown = svc.hl.unknown_coins(list(body.markets))
    if unknown:
        raise ValidationFailed("unknown Hyperliquid markets", markets=unknown)
    with svc.db.begin() as conn:
        row = svc.store.insert_strategy(conn, owner_user_id=ctx.user_id, slug=body.slug, name=body.name,
                                        description=body.description, markets=list(body.markets),
                                        timeframe=body.timeframe, price_monthly_micro=int(body.price_monthly_micro),
                                        profit_share_bps=body.profit_share_bps)
        if row is None:
            raise Conflict("this slug is taken")
        svc.audit.write(conn, actor=ctx.actor, action="creator.strategy.create", target=f"strategy:{row['id']}",
                        payload={"slug": body.slug, "markets": list(body.markets),
                                 "price_monthly_micro": int(body.price_monthly_micro),
                                 "profit_share_bps": body.profit_share_bps}, ip_hash=ctx.ip_hash)
    return _strategy_out(row)


@router.patch("/strategies/{strategy_id}", response_model=S.CreatorStrategyOut,
              dependencies=[user_limit("creator_strategy_patch", 20, 3600)])
def patch_strategy(strategy_id: UUID, body: S.CreatorStrategyPatchIn, ctx: AuthCtx = Depends(creator_step_up),
                   svc: Services = Depends(get_services)) -> S.CreatorStrategyOut:
    """Terms can change only before listing (subscribers agreed to the listed terms)."""
    _check_terms(svc, body.profit_share_bps, body.price_monthly_micro)
    with svc.db.begin() as conn:
        st = _owned(conn, svc, ctx, str(strategy_id), for_update=True)
        if st["status"] not in EDITABLE_STATUSES:
            raise Conflict("listed strategies cannot change their terms", status=st["status"])
        svc.store.update_strategy_terms(conn, str(strategy_id), name=body.name, description=body.description,
                                        price_monthly_micro=body.price_monthly_micro,
                                        profit_share_bps=body.profit_share_bps)
        svc.audit.write(conn, actor=ctx.actor, action="creator.strategy.update", target=f"strategy:{strategy_id}",
                        payload=body.model_dump(mode="json", exclude_none=True), ip_hash=ctx.ip_hash)
        return _strategy_out(svc.store.get_strategy(conn, str(strategy_id)))


@router.get("/strategies/{strategy_id}/versions", response_model=list[S.CreatorVersionOut])
def list_versions(strategy_id: UUID, ctx: AuthCtx = Depends(creator_user),
                  svc: Services = Depends(get_services)) -> list[S.CreatorVersionOut]:
    with svc.db.begin() as conn:
        _owned(conn, svc, ctx, str(strategy_id))
        return [version_out(r) for r in svc.store.list_versions(conn, str(strategy_id))]


@router.post("/strategies/{strategy_id}/versions", response_model=S.CreatorVersionOut, status_code=201,
             dependencies=[user_limit("creator_upload", 10, 3600)])
def upload_version(strategy_id: UUID, body: S.VersionUploadIn, ctx: AuthCtx = Depends(creator_step_up),
                   svc: Services = Depends(get_services)) -> S.CreatorVersionOut:
    sid = str(strategy_id)
    with svc.db.begin() as conn:
        st = _owned(conn, svc, ctx, sid)
        if st["status"] == "delisted":
            raise Conflict("delisted strategies cannot receive new versions")
    code = body.code if body.source == "python" else svc.sandbox.compile_nocode(body.spec or {})
    result = svc.sandbox.validate(code, None)
    if not result.get("ok"):
        raise ValidationFailed("strategy rejected by validation", errors=(result.get("errors") or [])[:50])
    meta = result["meta"]
    markets = list(meta["markets"])
    if not set(markets) <= set(st["markets"] or []) or meta["timeframe"] != st["timeframe"]:
        raise ValidationFailed("script MARKETS/TIMEFRAME must match the strategy",
                               strategy_markets=list(st["markets"] or []), strategy_timeframe=st["timeframe"])
    unknown = svc.hl.unknown_coins(markets)
    if unknown:
        raise ValidationFailed("unknown Hyperliquid markets", markets=unknown)
    report = _finite(svc.sandbox.backtest(code, meta))
    raw = code.encode("utf-8")
    code_hash = hashlib.sha256(raw).hexdigest()
    ciphertext, key_version = svc.code_vault.seal(raw, f"strategy_code:{sid}:{code_hash}".encode())
    params: dict[str, Any] = {"source": body.source, "kms_key_version": key_version}
    if body.source == "nocode":
        params["nocode_spec"] = body.spec
    with svc.db.begin() as conn:
        st = _owned(conn, svc, ctx, sid, for_update=True)   # serialises version numbering per strategy
        version = svc.store.next_version_number(conn, sid)
        row = svc.store.insert_version(
            conn, strategy_id=sid, version=version, code_hash=code_hash, code_ciphertext=ciphertext, params=params,
            markets=markets, timeframe=meta["timeframe"], lookback=int(meta["lookback"]),
            max_leverage=max(1, int(math.floor(float(meta["max_leverage"])))), backtest=report)
        if st["status"] == "draft":
            svc.store.set_strategy_status(conn, sid, "review")
        svc.audit.write(conn, actor=ctx.actor, action="creator.version.upload", target=f"strategy:{sid}",
                        payload={"version": version, "code_hash": code_hash, "source": body.source},
                        ip_hash=ctx.ip_hash)
    return version_out(row)


@router.post("/posts", response_model=S.PostOut, status_code=201, dependencies=[user_limit("creator_post", 20, 3600)])
def create_post(body: S.CreatorPostIn, ctx: AuthCtx = Depends(creator_step_up),
                svc: Services = Depends(get_services)) -> S.PostOut:
    price = svc.domain.validate_post_price(int(body.price_micro))
    with svc.db.begin() as conn:
        slug = None
        if body.strategy_id is not None:
            slug = _owned(conn, svc, ctx, str(body.strategy_id))["slug"]
        if price > 0:
            kyc = svc.store.get_kyc(conn, ctx.user_id)
            if not kyc or kyc["status"] != "approved":
                raise Forbidden("complete creator KYC before selling posts", reason="kyc_required")
        row = svc.store.insert_post(conn, creator_id=ctx.user_id,
                                    strategy_id=str(body.strategy_id) if body.strategy_id else None,
                                    title=body.title, body=body.body, price_micro=price, now=svc.now())
        svc.audit.write(conn, actor=ctx.actor, action="creator.post.publish", target=f"post:{row['id']}",
                        payload={"price_micro": price}, ip_hash=ctx.ip_hash)
    return S.PostOut(id=row["id"], title=row["title"], price_micro=int(row["price_micro"]), strategy_slug=slug,
                     published_at=row["published_at"], body=row["body"], purchased=False)


@router.get("/earnings", response_model=S.EarningsOut)
def earnings(ctx: AuthCtx = Depends(creator_user), svc: Services = Depends(get_services)) -> S.EarningsOut:
    account = ledger_ops.creator_payable(ctx.user_id)
    with svc.db.begin() as conn:
        payable = -svc.ledger.balance(conn, account)
        total = svc.store.total_credited(conn, account)
        pending = svc.store.pending_payouts_total(conn, ctx.user_id)
        by_strategy = svc.store.active_subscribers_by_strategy(conn, ctx.user_id)
        recent = svc.store.ledger_history(conn, account, 20, None)[:20]
    return S.EarningsOut(
        payable_micro=payable, payouts_pending_micro=pending, total_earned_micro=total,
        by_strategy=[S.StrategyEarningsOut(strategy_id=r["strategy_id"], slug=r["slug"],
                                           active_subscribers=int(r["active_subscribers"] or 0),
                                           earned_micro=r.get("earned_micro")) for r in by_strategy],
        recent=[S.LedgerEntryOut(tx_id=r["tx_id"], kind=r["kind"], memo=r.get("memo"),
                                 amount_micro=-int(r["raw_amount_micro"]), created_at=r["created_at"]) for r in recent])


@router.post("/kyc/session", response_model=S.KycSessionOut, dependencies=[user_limit("kyc", 5, 3600)])
def kyc_session(ctx: AuthCtx = Depends(creator_user), svc: Services = Depends(get_services)) -> S.KycSessionOut:
    with svc.db.begin() as conn:
        kyc = svc.store.get_kyc(conn, ctx.user_id)
    if kyc and kyc["status"] == "approved":
        raise Conflict("KYC already approved")
    session = svc.kyc.create_session(user_id=ctx.user_id, return_url=f"{svc.settings.web_origin}/#/creator/kyc")
    with svc.db.begin() as conn:
        svc.store.upsert_kyc_pending(conn, user_id=ctx.user_id, provider=str(session["provider"]),
                                     provider_ref=str(session["provider_ref"]))
        svc.audit.write(conn, actor=ctx.actor, action="creator.kyc.session", target=f"user:{ctx.user_id}",
                        payload={"provider": str(session["provider"])}, ip_hash=ctx.ip_hash)
    return S.KycSessionOut(url=str(session["url"]), provider=str(session["provider"]),
                           status=str(session.get("status") or "pending"))
