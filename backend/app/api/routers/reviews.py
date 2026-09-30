"""POST /reviews — only after ≥ 30 days SUBSCRIBED to the strategy (SPEC §4 reviews; REVIEW_AUTH_API F14: the total
time the user actually held a subscription — pending excluded, a cancelled one counts only until it ended — not
"30 days since a subscription that was cancelled a minute later"). One review per user and strategy (re-posting
edits it). Reads are public: GET /public/strategies/{slug}/reviews."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, get_services, user_limit
from app.errors import Forbidden, NotFound

router = APIRouter(prefix="/reviews", tags=["reviews"])

ELIGIBLE_AFTER = timedelta(days=30)


@router.post("", response_model=S.ReviewOut, status_code=201, dependencies=[user_limit("reviews", 10, 3600)])
def post_review(body: S.ReviewIn, ctx: AuthCtx = Depends(consented_user),
                svc: Services = Depends(get_services)) -> S.ReviewOut:
    sid = str(body.strategy_id)
    with svc.db.begin() as conn:
        st = svc.store.get_strategy(conn, sid)
        if st is None or st["status"] not in ("listed", "paused", "delisted"):
            raise NotFound("strategy not found")
        now = svc.now()
        held = timedelta(seconds=svc.store.subscribed_seconds(conn, ctx.user_id, sid, now))
        if held < ELIGIBLE_AFTER:
            raise Forbidden("reviews open after 30 days subscribed to this strategy",
                            subscribed_days=held.days)
        row = svc.store.upsert_review(conn, strategy_id=sid, user_id=ctx.user_id, rating=body.rating,
                                      body=body.body or None, eligible_since=now - (held - ELIGIBLE_AFTER))
    return S.ReviewOut(id=row["id"], rating=row["rating"], body=row.get("body"),
                       author=ctx.user.get("display_name") or "You", created_at=row["created_at"])
