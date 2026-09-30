"""Public (anonymous) routes. Rate-limited per IP; cacheable 60 s (set by RequestContextMiddleware).

Privacy (SPEC §5.8): stats are aggregated per strategy version and shown only with ≥ min_subscribers distinct
users (k-anonymity, app.domain.track_record.public_stats); subscriber addresses are never exposed (showcase
wallets only after their month ends). Public backtests omit the trade list and the latest signal weights.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api import schemas as S
from app.api import validation as v
from app.api.deps import Services, decode_cursor_or_422, get_services, ip_limit, next_cursor
from app.errors import NotFound

router = APIRouter(prefix="/public", tags=["public"], dependencies=[ip_limit("public", 120, 60)])

UNPROVEN_WARNING = "Backtest of a newly uploaded script can be fitted to history; not proven live yet"
_PUBLIC_BACKTEST_DROP = ("trades", "latest_signal", "data_notes")
SLUG_PATTERN = v.SLUG_RE.pattern

# SPEC §12 (owner, 30 Sep 2026): SILVER is a FREE showcase; its card and page say so plainly.
SHOWCASE_GENERIC = ("Free showcase of the engine: $0/month and 0% profit share. The 0.1% builder fee still applies "
                    "to any orders placed for you.")
SHOWCASE_TEXT = {
    "silver": (SHOWCASE_GENERIC + " The live signal has been CASH since 1980-01-15 under the current setting (the M2 "
               "filter is blocking entries), so subscribers may see no trades for a long time."),
}


def _history_days(version: Optional[dict]) -> Optional[int]:
    """Backtestable history of a version: the report's `history_days` (data-jobs helper) when present, else the
    simulated span `period.sim_days`. None = no report (in-house feed strategies: decades of terminal history)."""
    bt = (version or {}).get("backtest")
    if not isinstance(bt, dict):
        return None
    hd = bt.get("history_days")
    if isinstance(hd, (int, float)) and not isinstance(hd, bool) and math.isfinite(hd):
        return int(hd)
    days = (bt.get("period") or {}).get("sim_days") if isinstance(bt.get("period"), dict) else None
    if isinstance(days, (int, float)) and not isinstance(days, bool) and math.isfinite(days):
        return int(math.floor(days))
    return None


def _risk_ack_text(s: S.StrategySummary, max_lev_x: Optional[int]) -> str:
    markets = ", ".join(s.markets) or "its markets"
    lev = f"up to {max_lev_x}× leverage" if max_lev_x else "leverage"
    parts = [f"I understand that {s.name} trades {markets} perpetual futures on Hyperliquid in my own account with "
             f"{lev} on the allocation I choose; that it can lose some or all of that allocation; that backtests and "
             "past results do not predict future results; and that a new script version resets its live track record."]
    if s.not_live_proven:
        parts.append("It is not proven live yet.")
    if s.short_history_days is not None:
        parts.append(f"Its backtest covers only {s.short_history_days} days of history.")
    if s.showcase_text:
        parts.append(s.showcase_text)
    return " ".join(parts)


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
    econ, risk, now = svc.settings.economics, svc.settings.risk, svc.now()
    out = []
    for r in rows:
        sid = str(r["id"])
        ver = versions.get(sid)
        stats, not_proven = _stats(conn, svc, sid, ver, now, None)
        hold = holds.get(sid)
        live_since = ver.get("live_since") if ver else None
        history = _history_days(ver)
        price, ps = r["price_monthly_micro"], int(r["profit_share_bps"] or 0)
        showcase = bool(r["in_house"]) and price == 0 and ps == 0
        out.append(S.StrategySummary(
            id=r["id"], slug=r["slug"], name=r["name"], description=r.get("description"), in_house=r["in_house"],
            markets=list(r["markets"] or []), timeframe=r["timeframe"], status=r["status"],
            price_monthly_micro=price, profit_share_bps=ps, platform_profit_share_bps=econ.platform_profit_share_bps,
            platform_profit_share_mode=econ.platform_profit_share_mode, holds=hold,
            signal_state="unknown" if hold is None else ("holds" if hold else "trades"),
            current_version=ver["version"] if ver else None, live_since=live_since,
            live_days=max(0, (now - live_since).days) if live_since else None, not_live_proven=bool(not_proven),
            max_leverage=int(ver["max_leverage"]) if ver and ver.get("max_leverage") else None,
            history_days=history,
            short_history_days=history if history is not None and history < risk.short_history_warning_days else None,
            free_showcase=showcase,
            showcase_text=(SHOWCASE_TEXT.get(r["slug"], SHOWCASE_GENERIC) if showcase else None),
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
def strategy_detail(slug: str = Path(..., pattern=SLUG_PATTERN, max_length=64), svc: Services = Depends(get_services)) -> S.StrategyDetail:
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
        risk_ack_text=_risk_ack_text(summary, summary.max_leverage),
        versions=[S.StrategyVersionPublic(version=ver["version"], published_at=ver["published_at"],
                                          live_since=ver["live_since"], is_current=ver is current)
                  for ver in versions],
        backtest=_public_backtest(current.get("backtest") if current else None),
        backtest_warning=UNPROVEN_WARNING if not_proven else None,
        rating_avg_x100=rating.get("avg_x100"),
        rating_count=int(rating.get("n") or 0),
    )


@router.get("/strategies/{slug}/equity", response_model=S.EquitySeriesOut)
def strategy_equity(slug: str = Path(..., pattern=SLUG_PATTERN, max_length=64),
                    svc: Services = Depends(get_services)) -> S.EquitySeriesOut:
    """Daily aggregate LIVE record of the current version (fills closedPnl − fee + funding of all subscribers, the
    same inputs as `stats`): cumulative $ and ROI per UTC day since live_since. k-anonymity (SPEC §5.8): hidden with
    fewer than min_subscribers distinct users; days before k users had capital deployed are omitted."""
    with svc.db.begin() as conn:
        st = svc.store.get_public_strategy(conn, slug)
        if st is None:
            raise NotFound("strategy not found")
        sid = str(st["id"])
        ver = svc.store.current_versions(conn, [sid]).get(sid)
        live_since = ver.get("live_since") if ver else None
        if live_since is None:
            return S.EquitySeriesOut(slug=slug, version=ver["version"] if ver else None, hidden_reason="not_live")
        events, spans = svc.store.track_record_inputs(conn, sid, live_since)
    points, hidden = svc.domain.equity_series(live_since=live_since, events=events, spans=spans, now=svc.now(),
                                              min_subscribers=svc.settings.risk.min_subscribers_for_public_stats)
    return S.EquitySeriesOut(slug=slug, version=ver["version"], since=live_since,
                             points=[S.EquityPointOut(**p) for p in points], hidden_reason=hidden)


@router.get("/strategies/{slug}/reviews", response_model=S.Page[S.ReviewOut])
def strategy_reviews(slug: str = Path(..., pattern=SLUG_PATTERN, max_length=64), limit: int = Query(20, ge=1, le=50),
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


from app.api.caches import TtlCache  # noqa: E402

LEADERBOARD_CACHE = TtlCache(60.0)   # REVIEW_AUTH_API F10: per instance, single-flight per (by, period)


@router.get("/leaderboard", response_model=S.LeaderboardOut)
def leaderboard(by: Literal["roi", "pnl", "subscribers"] = Query("roi"),
                period: Literal["30d", "90d", "all"] = Query("30d"),
                svc: Services = Depends(get_services)) -> S.LeaderboardOut:
    return LEADERBOARD_CACHE.get_or_compute((id(svc), by, period), lambda: _leaderboard(svc, by, period))


def _leaderboard(svc: Services, by: str, period: str) -> S.LeaderboardOut:
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


@router.get("/posts/{post_id}", response_model=S.PostOut)
def public_post(post_id: UUID, svc: Services = Depends(get_services)) -> S.PostOut:
    """Anonymous read of a published post: the body only when the post is free (paid bodies need
    GET /v1/posts/{id} after purchase)."""
    with svc.db.begin() as conn:
        p = svc.store.get_post(conn, str(post_id))
    if p is None or p.get("published_at") is None:
        raise NotFound("post not found")
    free = int(p["price_micro"]) == 0
    return S.PostOut(id=p["id"], title=p["title"], price_micro=int(p["price_micro"]),
                     strategy_slug=p.get("strategy_slug"), published_at=p.get("published_at"),
                     body=p.get("body") if free else None, purchased=False)


@router.get("/config", response_model=S.PublicConfigOut)
def public_config(svc: Services = Depends(get_services)) -> S.PublicConfigOut:
    s, e, cfg = svc.settings, svc.settings.economics, svc.config
    bps = int(getattr(s, "stripe_fee_estimate_bps", 0) or 0)
    fixed = int(getattr(s, "stripe_fee_estimate_fixed_micro", 0) or 0)
    estimate_known = bps > 0 and not e.stripe_fee_absorbed
    return S.PublicConfigOut(
        builder_address=s.builder_address,
        treasury_address=s.treasury_address,
        agent_name=s.agent_name,
        hl_chain="Mainnet" if s.hl_is_mainnet else "Testnet",
        stripe_publishable_key=getattr(s, "stripe_publishable_key", None) or None,
        stripe_fee_estimate_bps=bps if estimate_known else None,
        stripe_fee_estimate_fixed_micro=fixed if estimate_known else None,
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
        max_user_leverage_x100=cfg.launch.max_user_leverage_x100,
        min_allocation_micro=S.MIN_ALLOCATION_MICRO,
        min_listing_history_days=s.risk.min_listing_history_days,
        short_history_warning_days=s.risk.short_history_warning_days,
        launch_phase=cfg.launch.phase,
    )


@router.get("/showcase/{slug}", response_model=list[S.ShowcaseWalletOut])
def showcase(slug: str = Path(..., pattern=SLUG_PATTERN, max_length=64), svc: Services = Depends(get_services)) -> list[S.ShowcaseWalletOut]:
    with svc.db.begin() as conn:
        rows = svc.store.showcase(conn, slug, svc.now())
    return [S.ShowcaseWalletOut(**r) for r in rows]
