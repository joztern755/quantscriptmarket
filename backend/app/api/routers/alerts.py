"""User alerts: GET /alerts, POST /alerts/{id}/ack."""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api import schemas as S
from app.api.deps import AuthCtx, Services, consented_user, decode_cursor_or_422, get_services, next_cursor
from app.errors import NotFound

router = APIRouter(prefix="/alerts", tags=["alerts"])


def alert_out(r: dict) -> S.AlertOut:
    return S.AlertOut(id=r["id"], severity=r["severity"], kind=r["kind"], payload=r.get("payload") or {},
                      created_at=r["created_at"], acked_at=r.get("acked_at"))


@router.get("", response_model=S.Page[S.AlertOut])
def list_alerts(limit: int = Query(50, ge=1, le=100), cursor: Optional[str] = Query(None, max_length=200),
                ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.Page[S.AlertOut]:
    cur = decode_cursor_or_422(cursor)
    with svc.db.begin() as conn:
        rows, nxt = next_cursor(svc.store.list_user_alerts(conn, ctx.user_id, limit, cur), limit)
    return S.Page[S.AlertOut](items=[alert_out(r) for r in rows], next_cursor=nxt)


@router.post("/{alert_id}/ack", response_model=S.Ok)
def ack_alert(alert_id: UUID, ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> S.Ok:
    with svc.db.begin() as conn:
        if not svc.store.ack_user_alert(conn, str(alert_id), ctx.user_id, svc.now()):
            raise NotFound("alert not found or already acknowledged")
    return S.Ok()
