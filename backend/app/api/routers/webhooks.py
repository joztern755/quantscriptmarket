"""POST /webhooks/stripe — RAW body, signature verified (app.payments.stripe_pay.verify_webhook, constant-time,
replay window), then the event is mapped to ledger instructions (handle_event) and applied in ONE transaction.

Idempotent: every instruction carries a ledger idempotency key (stripe:{pi}, stripe:refund:…, stripe:dispute:…)
and deposits.external_ref is unique, so Stripe's redeliveries are no-ops. A processing error returns 500 so
Stripe retries; an invalid signature returns 400 (never retried usefully). Exempt from the Cloudflare geo gate
(Stripe's servers are in the US) — the HMAC signature is the authentication.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from app.api import ledger_ops
from app.api.deps import Services, get_services, ip_limit
from app.errors import ValidationFailed
from app.logging import get_logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger("app.api.webhooks")
ACTOR = "stripe:webhook"


def _apply(svc: Services, outcome: Any) -> dict[str, Any]:
    with svc.db.begin() as conn:
        credited = [ledger_ops.apply_credit(conn, svc, c, actor=ACTOR) for c in outcome.credits]
        debited = [ledger_ops.apply_debit(conn, svc, d, actor=ACTOR) for d in outcome.debits]
        for alert in outcome.alerts:
            svc.notifier.notify_alert(conn, alert)
        svc.audit.write(conn, actor=ACTOR, action="stripe.event", target=f"stripe_event:{outcome.event_id}",
                        payload={"type": outcome.event_type, "credits": len(credited),
                                 "debits": sum(1 for d in debited if d), "ignored": outcome.ignored,
                                 "manual_review": outcome.manual_review}, ip_hash=None)
    return {"credits": len(credited), "debits": len(debited)}


@router.post("/stripe", dependencies=[ip_limit("webhook_stripe", 600, 60)], include_in_schema=False)
async def stripe_webhook(request: Request, svc: Services = Depends(get_services)) -> dict[str, bool]:
    payload = await request.body()   # raw bytes, exactly as signed (BodyLimitMiddleware replays them verbatim)
    sig = request.headers.get("stripe-signature")
    if not sig or len(sig) > 2048:
        raise ValidationFailed("missing Stripe-Signature header")
    event = await run_in_threadpool(svc.stripe.verify_webhook, payload, sig)
    outcome = await run_in_threadpool(svc.stripe.handle_event, event)
    await run_in_threadpool(_apply, svc, outcome)
    log.info("stripe event processed", extra={"fields": {"event_id": outcome.event_id, "type": outcome.event_type,
                                                         "ignored": outcome.ignored}})
    return {"received": True}
