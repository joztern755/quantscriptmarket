"""POST /v1/webhooks/telegram — Telegram Bot API webhook (SPEC §12 user alerts).

Authentication: the ``X-Telegram-Bot-Api-Secret-Token`` header must equal settings.telegram_webhook_secret
(constant-time; register it with setWebhook(secret_token=…, allowed_updates=["message", "my_chat_member"])).
Cloudflare additionally admits this path only from Telegram's published IP ranges (infra/cloudflare/dns.sh); it
is exempt from the geo gate and the edge-secret check (middleware.EdgeGuardMiddleware.EXEMPT_PATHS).

Handling (app.alerts.telegram_bot.handle_update): "/start <token>" links the chat to the token's user (token:
10 min, single use), "/stop" unlinks, bot blocked/unblocked (my_chat_member) marks the link lapsed/active again.
The reply is returned in the webhook response as a Bot API method call, so no outbound request is made here.
Malformed updates are acknowledged (200, no reply) so Telegram does not retry them forever; a database error
returns 500 so Telegram retries (every operation is idempotent: the token is single-use, /stop is a no-op twice).
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.alerts import telegram_bot
from app.api.deps import Services, get_services, ip_limit
from app.errors import Unauthorized
from app.logging import get_logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = get_logger("app.api.telegram")
MAX_UPDATE_BYTES = 64 * 1024


def _apply(svc: Services, update: dict[str, Any]) -> dict[str, Any] | None:
    with svc.db.begin() as conn:
        return telegram_bot.handle_update(conn, update, svc.now())


@router.post("/telegram", dependencies=[ip_limit("webhook_telegram", 600, 60)], include_in_schema=False)
async def telegram_webhook(request: Request, svc: Services = Depends(get_services)) -> JSONResponse:
    if not telegram_bot.secret_ok(request.headers.get("x-telegram-bot-api-secret-token"),
                                  svc.settings.telegram_webhook_secret):
        raise Unauthorized("invalid webhook secret")
    raw = await request.body()
    try:
        update = json.loads(raw) if raw and len(raw) <= MAX_UPDATE_BYTES else None
    except ValueError:
        update = None
    if not isinstance(update, dict):
        return JSONResponse({"ok": True})
    reply = await run_in_threadpool(_apply, svc, update)
    return JSONResponse(reply if reply is not None else {"ok": True})
