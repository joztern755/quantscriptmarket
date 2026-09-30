"""Public (anonymous) routes. Rate-limited per IP; cacheable 60 s (set by RequestContextMiddleware).

Privacy (SPEC §5.8): stats are aggregated per strategy version and shown only with ≥ min_subscribers distinct
users (k-anonymity, app.domain.track_record.public_stats); subscriber addresses are never exposed (showcase
wallets only after their month ends). Public backtests omit the trade list and the latest signal weights.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Path, Query

from app.api import schemas as S
from app.api import validation as v
from app.api.deps import Services, decode_cursor_or_422, get_services, ip_limit, next_cursor
from app.errors import NotFound

router = APIRouter(prefix="/public", tags=["public"], dependencies=[ip_limit("public", 120, 60)])

UNPROVEN_WARNING = "Backtest of a newly uploaded script can be fitted to history; not proven live yet"
_PUBLIC_BACKTEST_DROP = ("trades", "latest_signal", "data_notes")


def _slug() -> Any:
    return Path(..., pattern=v.SLUG_RE.pattern, max_length=64)


def _public_backtest(bt: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(bt, dict):
        return None
    return {k: val for k, val in bt.items() if k not in _PUBLIC_BACKTEST_DROP}


def _stats(conn: Any, svc: Services, strategy_id: str, version: Optional[dict], now: datetime,
           period_start: Optional[datetime]) -> tuple[S.StrategyStats, bool]:
    """(stats, not_live_proven)."""
    live_since = version.get("live_since") if version else None
    if live_since is None:
        return S.StrategyStats(hidden_reason="not_live"), True
    window_start = max(live_since, period_start) if period_start else live_since
    events, spans = svc.store.track_record_inputs(conn, strategy_id, window_start)
    tr = svc.domain.track_record(live_since=live_since, events=events, spans=spans, now=now,
                                 period_start=period_start,
                                 min_subscribers=svc.settings.risk.min_subscribers_for_public_stats)
    pub = tr["public"]
    if pub is None:
        return S.StrategyStats(since=window_start, hidden_reason="too_few_subscribers"), tr["not_live_proven"]
    return S.StrategyStats(subscribers=pub["subscribers"], roi_bps=pub["roi_bps"], pnl_micro=pub["pnl_micro"],
                           since=window_start), tr["not_live_proven"]


def _summaries(conn: Any, svc: Services, rows: list[dict]) -> list[S.StrategySummary]:
    ids = [str(r["id"]) for r in rows]
    versions = svc.store.current_versions(conn, ids)
    holds = svc.store.holds(conn, ids)
    econ, now = svc.settings.economics, svc.now()
    out = []
    for r in rows:
        sid = str(r["id"])
        ver = versions.get(sid)
        stats, _ = _stats(conn, svc, sid, ver, now, None)
        out.append(S.StrategySummary(
            id=r["id"], slug=r["slug"], name=r["name"], in_house=r["in_house"], markets=list(r["markets"] or []),
            timeframe=r["timeframe"], status=r["status"], price_monthly_micro=r["price_monthly_micro"],
            profit_share_bps=int(r["profit_share_bps"] or 0), platform_profit_share_bps=econ.platform_profit_share_bps,
            platform_profit_share_mode=econ.platform_profit_share_mode, holds=holds.get(sid),
            current_version=ver["version"] if ver else None, live_since=ver["live_since"] if ver else None,
            stats=stats))
    return out


@router.get("/strategies", response_model=S.Page[S.StrategySummary])
def list_strategies(
    market: Optional[str] = Query(None, pattern=v.COIN_RE.pattern, max_length=40),
    limit: int = Query(20, ge=1, le=50),
    cursor: Optional[str] = Query(None, max_length=200),
    svc: Services = Depends(get_services),
) -> S.Page[S.StrategySummary]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_public_strategies(conn, market=market, limit=limit, cursor=cur), limit)
        items = _summaries(conn, svc, rows)
    return S.Page[S.StrategySummary](items=items, next_cursor=nxt)


@router.get("/strategies/{slug}", response_model=S.StrategyDetail)
def strategy_detail(slug: str = _slug(), svc: Services = Depends(get_services)) -> S.StrategyDetail:
    with svc.db.begin() as conn:
        st = svc.store.get_public_strategy(conn, slug)
        if st is None:
            raise NotFound("strategy not found")
        sid = str(st["id"])
        summary = _summaries(conn, svc, [st])[0]
        versions = [ver for ver in svc.store.list_versions(conn, sid) if ver.get("published_at")]
        current = versions[0] if versions else None
        _, not_proven = _stats(conn, svc, sid, current, svc.now(), None)
        rating = svc.store.rating_summary(conn, sid)
    return S.StrategyDetail(
        **summary.model_dump(),
        description=st.get("description"),
        versions=[S.StrategyVersionPublic(version=ver["version"], published_at=ver["published_at"],
                                          live_since=ver["live_since"], is_current=ver is current)
                  for ver in versions],
        backtest=_public_backtest(current.get("backtest") if current else None),
        backtest_warning=UNPROVEN_WARNING if not_proven else None,
        rating_avg_x100=rating.get("avg_x100"),
        rating_count=int(rating.get("n") or 0),
    )


@router.get("/strategies/{slug}/reviews", response_model=S.Page[S.ReviewOut])
def strategy_reviews(slug: str = _slug(), limit: int = Query(20, ge=1, le=50),
                     cursor: Optional[str] = Query(None, max_length=200),
                     svc: Services = Depends(get_services)) -> S.Page[S.ReviewOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        st = svc.store.get_public_strategy(conn, slug)
        if st is None:
            raise NotFound("strategy not found")
        rows, nxt = next_cursor(svc.store.list_reviews(conn, str(st["id"]), limit, cur), limit)
    return S.Page[S.ReviewOut](items=[S.ReviewOut(**r) for r in rows], next_cursor=nxt)


_PERIODS = {"30d": timedelta(days=30), "90d": timedelta(days=90), "all": None}


@router.get("/leaderboard", response_model=S.LeaderboardOut)
def leaderboard(by: Literal["roi", "pnl", "subscribers"] = Query("roi"),
                period: Literal["30d", "90d", "all"] = Query("30d"),
                svc: Services = Depends(get_services)) -> S.LeaderboardOut:
    now = svc.now()
    delta = _PERIODS[period]
    period_start = now - delta if delta else None
    entries: list[dict[str, Any]] = []
    with svc.db.begin() as conn:
        rows = svc.store.list_public_strategies(conn, market=None, limit=200, cursor=None)
        versions = svc.store.current_versions(conn, [str(r["id"]) for r in rows])
        for r in rows:
            stats, _ = _stats(conn, svc, str(r["id"]), versions.get(str(r["id"])), now, period_start)
            if stats.hidden_reason:   # k-anonymity / not live: never ranked
                continue
            entries.append({"slug": r["slug"], "name": r["name"], "roi_bps": stats.roi_bps,
                            "pnl_micro": stats.pnl_micro, "subscribers": stats.subscribers})
    key = {"roi": "roi_bps", "pnl": "pnl_micro", "subscribers": "subscribers"}[by]
    entries.sort(key=lambda e: (e[key] is not None, e[key] or 0), reverse=True)
    return S.LeaderboardOut(by=by, period=period,
                            entries=[S.LeaderboardEntry(rank=i + 1, **e) for i, e in enumerate(entries[:100])])


@router.get("/posts", response_model=S.Page[S.PostSummary])
def public_posts(strategy: Optional[str] = Query(None, pattern=v.SLUG_RE.pattern, max_length=64),
                 limit: int = Query(20, ge=1, le=50), cursor: Optional[str] = Query(None, max_length=200),
                 svc: Services = Depends(get_services)) -> S.Page[S.PostSummary]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_public_posts(conn, strategy_slug=strategy, limit=limit, cursor=cur),
                                limit)
    return S.Page[S.PostSummary](items=[S.PostSummary(**r) for r in rows], next_cursor=nxt)


@router.get("/config", response_model=S.PublicConfigOut)
def public_config(svc: Services = Depends(get_services)) -> S.PublicConfigOut:
    s, e, cfg = svc.settings, svc.settings.economics, svc.config
    pct = getattr(s, "stripe_fee_estimate_pct_bps", None)
    fixed = getattr(s, "stripe_fee_estimate_fixed_micro", None)
    return S.PublicConfigOut(
        builder_address=s.builder_address,
        treasury_address=s.treasury_address,
        agent_name=s.agent_name,
        hl_chain="Mainnet" if s.hl_is_mainnet else "Testnet",
        stripe_publishable_key=getattr(s, "stripe_publishable_key", None) or None,
        stripe_fee_estimate=(S.StripeFeeEstimateOut(pct_bps=int(pct), fixed_micro=int(fixed or 0))
                             if pct is not None and not e.stripe_fee_absorbed else None),
        restricted_jurisdictions=list(s.restricted_countries),
        legal_versions=dict(cfg.legal_versions),
        economics=S.EconomicsOut(
            builder_fee_tenths_bp=e.builder_fee_tenths_bp, builder_split_creator_bps=e.builder_split_creator_bps,
            builder_split_platform_bps=e.builder_split_platform_bps,
            builder_split_referral_pool_bps=e.builder_split_referral_pool_bps,
            profit_share_creator_cap_bps=e.profit_share_creator_cap_bps,
            platform_profit_share_bps=e.platform_profit_share_bps,
            platform_profit_share_mode=e.platform_profit_share_mode,
            subscription_platform_bps=e.subscription_platform_bps, post_platform_fee_micro=e.post_platform_fee_micro,
            post_min_price_micro=e.post_min_price_micro, min_topup_micro=e.min_topup_micro,
            past_due_grace_hours=e.past_due_grace_hours, stripe_fee_absorbed=e.stripe_fee_absorbed),
        plans=[S.PlanOut(key=p.key, price_monthly_micro=p.price_monthly_micro,
                         max_active_strategies=p.max_active_strategies, features=list(p.features)) for p in e.plans],
        referral_tiers=[S.ReferralTierOut(name=t.name, min_active_users=t.min_active_users,
                                          min_notional_30d_micro=t.min_notional_30d_micro,
                                          share_of_pool_bps=t.share_of_pool_bps) for t in e.referral_tiers],
        features={"creator_uploads": bool(s.feature_creator_uploads),
                  "payouts": cfg.launch.payouts_enabled},
        platform_max_leverage=s.risk.platform_max_leverage,
        min_allocation_micro=S.MIN_ALLOCATION_MICRO,
        launch_phase=cfg.launch.phase,
    )


@router.get("/showcase/{slug}", response_model=list[S.ShowcaseWalletOut])
def showcase(slug: str = _slug(), svc: Services = Depends(get_services)) -> list[S.ShowcaseWalletOut]:
    with svc.db.begin() as conn:
        rows = svc.store.showcase(conn, slug, svc.now())
    return [S.ShowcaseWalletOut(**r) for r in rows]
