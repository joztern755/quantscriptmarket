"""POST /webhooks/kyc — the KYC provider's verdict for a creator (app.kyc; Sumsub-style signed notification).

RAW body; the signature is verified by the provider module before anything is read. The verdict is re-read from the
provider's API (app.kyc.sumsub ``refetch``) and applied idempotently to ``kyc_creators`` only when the notification
matches the stored (provider, provider_ref) for that user. A bad signature is 400; a provider/API failure is 5xx
so the provider retries.

Owner decision (30 Sep 2026): a GREEN verdict is stored as ``provider_approved`` — NEVER auto-approved — and ops are
asked (``kyc_awaiting_admin``) for ONE admin to confirm via POST /v1/admin/users/{id}/kyc → ``approved``. RED →
``rejected`` at once. Manual KYC has no webhook: one admin records the verdict on the same endpoint. Only
``approved`` unlocks listing, paid posts and payouts (listing and payouts keep their two-admin rules).
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import ServiceUnavailable, Services, get_services, ip_limit
from app.errors import ValidationFailed
from app.logging import get_logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger("app.api.kyc_webhook")
ACTOR = "kyc:webhook"
_HEADERS = ("x-payload-digest", "x-payload-digest-alg", "content-type")


def _kyc() -> Any:
    try:
        return importlib.import_module("app.kyc")
    except ImportError:
        raise ServiceUnavailable("KYC provider not configured") from None


def _parse(svc: Services, payload: bytes, headers: dict[str, str]) -> Any:
    fn = getattr(svc.kyc, "parse_webhook", None)          # an adapter may provide it; else the module directly
    if callable(fn):
        return fn(payload, headers)
    return _kyc().parse_webhook(payload, headers, settings=svc.settings)


def _apply(svc: Services, event: Any) -> dict[str, Any]:
    kyc_mod = _kyc()
    with svc.db.begin() as conn:
        row = svc.store.get_kyc(conn, event.user_id)
        new = None
        if row is None or row["provider"] != event.provider or row["provider_ref"] != event.provider_ref:
            result = "unknown_applicant"
        else:
            new = kyc_mod.next_status(row["status"], event)
            result = "unchanged" if new is None else "updated"
            if new is not None:
                svc.store.set_kyc_status(conn, event.user_id, new)
                svc.notifier.notify(conn, user_id=event.user_id, severity="info", kind="kyc_status",
                                    payload={"status": new})
                if row["status"] == "approved":
                    svc.notifier.notify(conn, user_id=None, severity="warn", kind="kyc_approval_revoked",
                                        payload={"user_id": event.user_id, "status": new, "event": event.event_type})
                if new == getattr(kyc_mod, "PROVIDER_APPROVED", "provider_approved"):
                    svc.notifier.notify(conn, user_id=None, severity="warn", kind="kyc_awaiting_admin",
                                        payload={"user_id": event.user_id, "provider": event.provider,
                                                 "action": "confirm in the admin console (one admin)"})
        svc.audit.write(conn, actor=ACTOR, action="kyc.webhook", target=f"user:{event.user_id}",
                        payload={"provider": event.provider, "provider_ref": event.provider_ref,
                                 "event_type": event.event_type, "event_id": event.event_id,
                                 "status": event.status, "applied": new, "result": result,
                                 "reject_labels": list(event.reject_labels)}, ip_hash=None)
    return {"result": result}


@router.post("/kyc", dependencies=[ip_limit("webhook_kyc", 300, 60)], include_in_schema=False)
async def kyc_webhook(request: Request, svc: Services = Depends(get_services)) -> dict[str, bool]:
    payload = await request.body()   # raw bytes, exactly as signed
    headers = {k: v for k, v in request.headers.items() if k.lower() in _HEADERS}
    if not headers.get("x-payload-digest") or len(headers["x-payload-digest"]) > 256:
        raise ValidationFailed("missing payload digest header")
    event = await run_in_threadpool(_parse, svc, payload, headers)
    if event is None:
        return {"received": True}
    out = await run_in_threadpool(_apply, svc, event)
    log.info("kyc event processed", extra={"fields": {"type": event.event_type, "result": out["result"]}})
    return {"received": True}
