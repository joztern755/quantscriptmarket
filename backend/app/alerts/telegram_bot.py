"""Telegram linking: one-time /start tokens and the bot webhook update handler (SPEC §12).

Flow
  1. POST /v1/alerts/telegram/link (auth) → :func:`create_link` stores sha256(token) (10 min, single use, bound to
     the user) and returns ``https://t.me/<bot>?start=<token>``.
  2. The user taps it; Telegram sends "/start <token>" to our webhook POST /v1/webhooks/telegram, authenticated by
     the ``X-Telegram-Bot-Api-Secret-Token`` header (:func:`secret_ok`, constant-time; configure it with
     setWebhook(secret_token=TELEGRAM_WEBHOOK_SECRET)). :func:`handle_update` consumes the token and stores the
     chat id. Private chats only.
  3. "/stop" unlinks every account linked to that chat (a mandatory email tells the user that new entries pause
     after 24 h). A ``my_chat_member`` update with status "kicked" (user blocked the bot) marks the link blocked;
     unblocking (status "member") or a tokenless "/start" from the same chat re-activates a *blocked* link.

Replies are returned as a webhook-response method call ({"method": "sendMessage", ...}) so the webhook needs no
outbound request. Nothing here logs tokens, chat ids or message text.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from app.errors import RateLimited
from app.logging import get_logger

from . import _db
from .user_sinks import AlertsUnavailable, queue_unreachable_alert

log = get_logger("app.alerts.telegram")

__all__ = ["LINK_TTL", "create_link", "secret_ok", "handle_update", "token_hash"]

LINK_TTL = timedelta(minutes=10)
LINKS_PER_HOUR = 10
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_BOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

MSG_LINKED = ("Linked. You will receive your aijalon.trade alerts here (trades, PnL, fee balance, security).\n"
              "Send /stop to unlink — note that alerts are required while you have subscriptions: new entries "
              "pause 24 hours after Telegram alerts stop.")
MSG_BAD_TOKEN = ("This link has expired or was already used. Open aijalon.trade → Alerts and tap "
                 "\"Link Telegram\" to get a new one (links are valid for 10 minutes).")
MSG_HELLO = "To receive alerts, open aijalon.trade → Alerts and tap \"Link Telegram\"."
MSG_RESUMED = "Welcome back — your aijalon.trade alerts are active again in this chat."
MSG_STOPPED = ("Unlinked. You will no longer receive aijalon.trade alerts here. Telegram alerts are required "
               "while you have subscriptions: new entries pause in 24 hours unless you link again "
               "(aijalon.trade → Alerts).")
MSG_NOT_LINKED = "This chat is not linked to an aijalon.trade account."
MSG_PRIVATE = "Please message me in a private chat."


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def secret_ok(provided: Optional[str], configured: str) -> bool:
    """Constant-time check of X-Telegram-Bot-Api-Secret-Token. An unconfigured secret rejects everything."""
    if not configured or not provided or len(provided) > 256:
        return False
    return hmac.compare_digest(provided.encode(), configured.encode())


def create_link(conn: Any, *, user_id: str, now: datetime, bot_username: str) -> dict[str, Any]:
    bot = (bot_username or "").lstrip("@")
    if not _BOT_RE.match(bot):
        raise AlertsUnavailable("Telegram bot is not configured")
    n = _db.one(conn, """SELECT count(*) AS n FROM telegram_link_tokens
                         WHERE user_id = CAST(:u AS uuid) AND created_at > CAST(:since AS timestamptz)""",
                u=user_id, since=now - timedelta(hours=1))
    if n and int(n["n"]) >= LINKS_PER_HOUR:
        raise RateLimited("too many link requests; try again later", retry_after_seconds=600)
    token = secrets.token_urlsafe(24)          # 32 chars of [A-Za-z0-9_-] (Telegram start payload ≤ 64)
    expires = now + LINK_TTL
    _db.rows(conn, """INSERT INTO telegram_link_tokens (token_hash, user_id, created_at, expires_at)
                      VALUES (:h, CAST(:u AS uuid), CAST(:now AS timestamptz), CAST(:exp AS timestamptz))
                      RETURNING token_hash""", h=token_hash(token), u=user_id, now=now, exp=expires)
    return {"url": f"https://t.me/{bot}?start={token}", "expires_at": expires}


def _reply(chat_id: int, text: str) -> dict[str, Any]:
    return {"method": "sendMessage", "chat_id": chat_id, "text": text, "disable_web_page_preview": True}


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,20}", v):
        return int(v)
    return None


def _link(conn: Any, chat_id: int, token: str, now: datetime) -> Optional[str]:
    """Consume the token (atomic, single use) and bind the chat. Returns the user id or None."""
    r = _db.one(conn, """UPDATE telegram_link_tokens SET used_at = CAST(:now AS timestamptz), used_chat_id = :chat
                          WHERE token_hash = :h AND used_at IS NULL AND expires_at > CAST(:now AS timestamptz)
                          RETURNING user_id""", h=token_hash(token), now=now, chat=chat_id)
    if r is None:
        return None
    uid = str(r["user_id"])
    _db.rows(conn, """
        INSERT INTO user_contacts (user_id, created_at, updated_at, telegram_chat_id, telegram_linked_at)
        VALUES (CAST(:u AS uuid), CAST(:now AS timestamptz), CAST(:now AS timestamptz), :chat, CAST(:now AS timestamptz))
        ON CONFLICT (user_id) DO UPDATE
           SET telegram_chat_id = EXCLUDED.telegram_chat_id, telegram_linked_at = EXCLUDED.telegram_linked_at,
               telegram_blocked_at = NULL, telegram_block_reason = NULL, updated_at = EXCLUDED.updated_at
        RETURNING user_id""", u=uid, now=now, chat=chat_id)
    return uid


def _reactivate(conn: Any, chat_id: int, now: datetime) -> int:
    rows = _db.rows(conn, """UPDATE user_contacts SET telegram_blocked_at = NULL, telegram_block_reason = NULL,
                                    updated_at = CAST(:now AS timestamptz)
                              WHERE telegram_chat_id = :chat AND telegram_block_reason IN ('blocked', 'chat_not_found')
                              RETURNING user_id""", chat=chat_id, now=now)
    return len(rows)


def _stop(conn: Any, chat_id: int, now: datetime) -> list[str]:
    rows = _db.rows(conn, """UPDATE user_contacts SET telegram_chat_id = NULL, telegram_linked_at = NULL,
                                    telegram_blocked_at = CAST(:now AS timestamptz), telegram_block_reason = 'stopped',
                                    updated_at = CAST(:now AS timestamptz)
                              WHERE telegram_chat_id = :chat
                              RETURNING user_id""", chat=chat_id, now=now)
    uids = [str(r["user_id"]) for r in rows]
    for uid in uids:
        queue_unreachable_alert(conn, user_id=uid, now=now, reason="stopped")
    return uids


def _blocked(conn: Any, chat_id: int, now: datetime) -> list[str]:
    rows = _db.rows(conn, """UPDATE user_contacts SET telegram_blocked_at = CAST(:now AS timestamptz),
                                    telegram_block_reason = 'blocked', updated_at = CAST(:now AS timestamptz)
                              WHERE telegram_chat_id = :chat AND telegram_blocked_at IS NULL
                              RETURNING user_id""", chat=chat_id, now=now)
    uids = [str(r["user_id"]) for r in rows]
    for uid in uids:
        queue_unreachable_alert(conn, user_id=uid, now=now, reason="blocked")
    return uids


def handle_update(conn: Any, update: Any, now: datetime) -> Optional[dict[str, Any]]:
    """Apply one Telegram Update. Returns a webhook-response reply (or None). Never raises on odd input."""
    if not isinstance(update, dict):
        return None
    mcm = update.get("my_chat_member")
    if isinstance(mcm, dict):
        chat = mcm.get("chat") if isinstance(mcm.get("chat"), dict) else {}
        chat_id = _int(chat.get("id"))
        status = str(((mcm.get("new_chat_member") or {}) if isinstance(mcm.get("new_chat_member"), dict) else {})
                     .get("status") or "")
        if chat_id is None or chat.get("type") != "private":
            return None
        if status == "kicked":
            n = len(_blocked(conn, chat_id, now))
            log.info("telegram bot blocked by user", extra={"fields": {"accounts": n}})
        elif status == "member":
            _reactivate(conn, chat_id, now)
        return None

    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
    chat_id = _int(chat.get("id"))
    text = msg.get("text")
    if chat_id is None or not isinstance(text, str):
        return None
    if chat.get("type") != "private":
        return _reply(chat_id, MSG_PRIVATE) if text.startswith("/") else None
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd == "/start":
        if arg:
            if not _TOKEN_RE.match(arg):
                return _reply(chat_id, MSG_BAD_TOKEN)
            uid = _link(conn, chat_id, arg, now)
            if uid is None:
                return _reply(chat_id, MSG_BAD_TOKEN)
            log.info("telegram linked", extra={"fields": {"user_id": uid}})
            return _reply(chat_id, MSG_LINKED)
        if _reactivate(conn, chat_id, now):
            return _reply(chat_id, MSG_RESUMED)
        return _reply(chat_id, MSG_HELLO)
    if cmd == "/stop":
        uids = _stop(conn, chat_id, now)
        if uids:
            log.info("telegram unlinked by /stop", extra={"fields": {"accounts": len(uids)}})
            return _reply(chat_id, MSG_STOPPED)
        return _reply(chat_id, MSG_NOT_LINKED)
    if cmd in ("/help", "/status"):
        linked = _db.one(conn, """SELECT count(*) AS n FROM user_contacts
                                   WHERE telegram_chat_id = :chat AND telegram_blocked_at IS NULL""", chat=chat_id)
        if linked and int(linked["n"]) > 0:
            return _reply(chat_id, "This chat receives aijalon.trade alerts. Send /stop to unlink.")
        return _reply(chat_id, MSG_HELLO)
    return None
