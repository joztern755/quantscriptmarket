"""Per-user alert contacts (Telegram chat + confirmed email), the mandatory-contacts gate, the user-facing
Telegram Bot API client and email helpers (SPEC §12 "User alerts on Telegram + email").

Contacts model (migrations/0007_alerts.sql, table user_contacts)
  telegram  linked   chat id stored, not blocked
            blocked  Telegram answered 403 (user blocked the bot) / chat not found: chat id kept so a tokenless
                     /start from the same chat re-activates it
            stopped  the user sent /stop: chat id cleared; a new one-time link is needed
            unlinked never linked
  email     NULL until confirmed. Default = the account email, confirmed in one click when the identity
            provider verified it (Google / Apple, private-relay addresses included). Any other address needs a
            6-digit code sent to it (HMAC-hashed, 10 min, 5 attempts) — the change is started with step-up.

Mandatory-contacts gate
  * POST /v1/subscriptions refuses with 409 ``contacts_required`` unless Telegram is linked (not blocked /
    stopped) AND an email is confirmed (:func:`require_alert_contacts`).
  * Executor contract — NEW ENTRIES ONLY (exits always run): a user's subscriptions may open / increase
    positions only while ``alert_contacts_entries_allowed(user_id, now)`` (SQL function, 0007) is TRUE, i.e.
    confirmed email AND (working Telegram link OR a link that lapsed less than 24 h ago). The executor should
    evaluate it once per user per tick (e.g. ``JOIN LATERAL`` / ``WHERE alert_contacts_entries_allowed(s.user_id,
    :now)`` in its due-entries query, or :func:`entries_allowed` in Python) and treat a FALSE user like
    ``reduce_only`` for that tick. Nothing here edits execution code.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from app.errors import AppError, Conflict, RateLimited, ValidationFailed
from app.logging import get_logger

from . import _db
from .notifier import (
    EmailProvider,
    EmailSink,
    PermanentSinkError,
    ResendProvider,
    TransientSinkError,
    _default_session,
    mask_email,
)

log = get_logger("app.alerts.user")

__all__ = [
    "ContactsRequired", "AlertsUnavailable", "ContactStatus", "TelegramBlocked", "TelegramBotApi", "BLOCK_GRACE",
    "contact_status", "require_alert_contacts", "entries_allowed", "confirm_account_email",
    "start_email_verification", "verify_email_code", "EmailVerifyResult", "normalize_email",
    "mark_telegram_blocked", "email_provider", "telegram_api", "NoUserEmailContacts", "EMAIL_CODE_TTL", "EMAIL_CODE_MAX_ATTEMPTS",
]

BLOCK_GRACE = timedelta(hours=24)         # after the Telegram link lapses, new entries pause after this
EMAIL_CODE_TTL = timedelta(minutes=10)
EMAIL_CODE_MAX_ATTEMPTS = 5
EMAIL_CODES_PER_HOUR = 5
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class ContactsRequired(AppError):
    """Subscribing needs a linked Telegram chat AND a confirmed email (SPEC §12)."""
    http_status, code = 409, "contacts_required"


class AlertsUnavailable(AppError):
    """Email / Telegram delivery not configured or temporarily failing (maps to 503)."""
    http_status, code = 503, "service_unavailable"


# ============================================================================================ status
@dataclass(frozen=True)
class ContactStatus:
    telegram: str                       # linked | blocked | stopped | unlinked
    telegram_linked_at: Optional[datetime]
    telegram_blocked_at: Optional[datetime]
    email: Optional[str]                # confirmed alert email (the user's own; shown to them only)
    email_verified: bool
    pending_email: Optional[str]        # address a live code was sent to
    account_email: Optional[str]
    entries_allowed: bool

    @property
    def telegram_ok(self) -> bool:
        return self.telegram == "linked"

    @property
    def ready(self) -> bool:
        return self.telegram_ok and self.email_verified

    @property
    def entries_pause_at(self) -> Optional[datetime]:
        if self.telegram in ("blocked", "stopped") and self.telegram_blocked_at is not None:
            return self.telegram_blocked_at + BLOCK_GRACE
        return None

    def missing(self) -> list[str]:
        out = []
        if not self.telegram_ok:
            out.append("telegram")
        if not self.email_verified:
            out.append("email")
        return out

    def public(self) -> dict[str, Any]:
        pause = self.entries_pause_at
        return {
            "telegram": {"status": self.telegram,
                         "linked_at": self.telegram_linked_at.isoformat() if self.telegram_linked_at else None,
                         "lapsed_at": self.telegram_blocked_at.isoformat() if self.telegram_blocked_at else None},
            "email": {"address": self.email, "verified": self.email_verified, "pending": self.pending_email,
                      "account_email": self.account_email},
            "ready": self.ready,
            "missing": self.missing(),
            "entries_allowed": self.entries_allowed,
            "entries_pause_at": pause.isoformat() if pause else None,
        }


def _tg_state(row: Optional[dict]) -> str:
    if not row:
        return "unlinked"
    reason = row.get("telegram_block_reason")
    if row.get("telegram_chat_id") is not None and row.get("telegram_blocked_at") is None:
        return "linked"
    if reason == "stopped":
        return "stopped"
    if reason in ("blocked", "chat_not_found"):
        return "blocked"
    return "unlinked"


def contact_status(conn: Any, user_id: str, now: datetime) -> ContactStatus:
    row = _db.one(conn, """
        SELECT u.email AS account_email, c.telegram_chat_id, c.telegram_linked_at, c.telegram_blocked_at,
               c.telegram_block_reason, c.email, c.email_verified_at,
               alert_contacts_entries_allowed(u.id, CAST(:now AS timestamptz)) AS entries_allowed,
               (SELECT e.email FROM email_verification_codes e
                 WHERE e.user_id = u.id AND e.consumed_at IS NULL AND e.superseded_at IS NULL
                   AND e.expires_at > CAST(:now AS timestamptz) AND e.attempts < :max_attempts
                 ORDER BY e.created_at DESC LIMIT 1) AS pending_email
          FROM users u LEFT JOIN user_contacts c ON c.user_id = u.id
         WHERE u.id = CAST(:u AS uuid)""", u=user_id, now=now, max_attempts=EMAIL_CODE_MAX_ATTEMPTS)
    row = row or {}
    return ContactStatus(
        telegram=_tg_state(row),
        telegram_linked_at=_db.ts(row.get("telegram_linked_at")),
        telegram_blocked_at=_db.ts(row.get("telegram_blocked_at")),
        email=row.get("email") if row.get("email_verified_at") else None,
        email_verified=row.get("email_verified_at") is not None,
        pending_email=row.get("pending_email"),
        account_email=row.get("account_email"),
        entries_allowed=bool(row.get("entries_allowed")),
    )


def entries_allowed(conn: Any, user_id: str, now: datetime) -> bool:
    """Executor helper (same rule as the SQL function alert_contacts_entries_allowed)."""
    r = _db.one(conn, "SELECT alert_contacts_entries_allowed(CAST(:u AS uuid), CAST(:now AS timestamptz)) AS ok",
                u=user_id, now=now)
    return bool(r and r.get("ok"))


def require_alert_contacts(conn: Any, svc: Any, user_id: str) -> None:
    """Gate for POST /v1/subscriptions. ``svc.store.alert_contacts_ready`` (test fakes) takes precedence."""
    fake = getattr(getattr(svc, "store", None), "alert_contacts_ready", None)
    if callable(fake):
        if not fake(conn, user_id):
            raise ContactsRequired("link Telegram and confirm your email for alerts before subscribing",
                                   missing=["telegram", "email"])
        return
    st = contact_status(conn, user_id, svc.now())
    if not st.ready:
        raise ContactsRequired("link Telegram and confirm your email for alerts before subscribing",
                               missing=st.missing(), telegram=st.telegram)


# ============================================================================================ email
def normalize_email(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValidationFailed("invalid email address")
    e = raw.strip()
    if len(e) > 254 or not _EMAIL_RE.fullmatch(e) or any(ord(ch) < 33 or ord(ch) == 127 for ch in e):
        raise ValidationFailed("invalid email address")
    local, _, domain = e.rpartition("@")
    return f"{local}@{domain.lower()}"


def _ensure_row(conn: Any, user_id: str, now: datetime) -> None:
    _db.rows(conn, """INSERT INTO user_contacts (user_id, created_at, updated_at)
                      VALUES (CAST(:u AS uuid), CAST(:now AS timestamptz), CAST(:now AS timestamptz))
                      ON CONFLICT (user_id) DO NOTHING RETURNING user_id""", u=user_id, now=now)


def _set_verified_email(conn: Any, user_id: str, email: str, now: datetime) -> Optional[str]:
    """Store the confirmed email; returns the previous confirmed address (None if none / unchanged)."""
    _ensure_row(conn, user_id, now)
    prev = _db.one(conn, "SELECT email, email_verified_at FROM user_contacts WHERE user_id = CAST(:u AS uuid) FOR UPDATE",
                   u=user_id)
    _db.rows(conn, """UPDATE user_contacts SET email = :e, email_verified_at = CAST(:now AS timestamptz),
                             updated_at = CAST(:now AS timestamptz)
                       WHERE user_id = CAST(:u AS uuid) RETURNING user_id""", u=user_id, e=email, now=now)
    old = (prev or {}).get("email") if (prev or {}).get("email_verified_at") else None
    return old if old and old.lower() != email.lower() else None


def confirm_account_email(conn: Any, *, user_id: str, claims_email: Any, claims_email_verified: Any,
                          now: datetime) -> str:
    """One-click confirmation of the ACCOUNT email: only when the identity provider verified it (token claim
    ``email_verified is True``) and it equals the email on the user row."""
    acct = _db.one(conn, "SELECT email FROM users WHERE id = CAST(:u AS uuid)", u=user_id)
    acct_email = (acct or {}).get("email")
    if not acct_email or claims_email_verified is not True or not isinstance(claims_email, str) \
            or claims_email.strip().lower() != str(acct_email).strip().lower():
        raise Conflict("your sign-in email is not verified by the provider; verify an address with a code instead",
                       reason="email_not_verified")
    email = normalize_email(str(acct_email))
    _set_verified_email(conn, user_id, email, now)
    return email


def _code_hash(pepper: bytes, user_id: str, email: str, code: str) -> str:
    msg = f"aijalon-email-code:v1:{user_id}:{email.lower()}:{code}".encode()
    return hmac.new(pepper or b"aijalon-dev-pepper", msg, hashlib.sha256).hexdigest()


def start_email_verification(conn: Any, *, user_id: str, email: str, now: datetime, pepper: bytes,
                             provider: Optional[EmailProvider]) -> datetime:
    """Supersede any live code, store a new 6-digit code (hashed) and email it to ``email``. Call with step-up.
    Raises RateLimited after EMAIL_CODES_PER_HOUR codes in the last hour; ServiceUnavailable-like errors from
    the provider propagate (the caller's transaction rolls back, so no dangling code)."""
    email = normalize_email(email)
    if provider is None:
        raise AlertsUnavailable("email delivery is not configured")
    n = _db.one(conn, """SELECT count(*) AS n FROM email_verification_codes
                         WHERE user_id = CAST(:u AS uuid) AND created_at > CAST(:since AS timestamptz)""",
                u=user_id, since=now - timedelta(hours=1))
    if n and int(n["n"]) >= EMAIL_CODES_PER_HOUR:
        raise RateLimited("too many verification codes; try again later", retry_after_seconds=3600)
    code = f"{secrets.randbelow(10**6):06d}"
    expires = now + EMAIL_CODE_TTL
    _db.rows(conn, """UPDATE email_verification_codes SET superseded_at = CAST(:now AS timestamptz)
                       WHERE user_id = CAST(:u AS uuid) AND consumed_at IS NULL AND superseded_at IS NULL
                       RETURNING id""", u=user_id, now=now)
    _db.rows(conn, """INSERT INTO email_verification_codes (user_id, email, code_hash, created_at, expires_at)
                      VALUES (CAST(:u AS uuid), :e, :h, CAST(:now AS timestamptz), CAST(:exp AS timestamptz))
                      RETURNING id""", u=user_id, e=email, h=_code_hash(pepper, user_id, email, code), now=now,
             exp=expires)
    try:
        provider.send(email, "[aijalon] Your verification code",
                      f"Your aijalon.trade verification code is {code}.\n\nIt expires in 10 minutes. Enter it on the "
                      "Alerts page to receive alerts at this address.\nIf you did not request this, ignore this "
                      "email — nothing changes." + EmailSink.FOOTER)
    except TransientSinkError:
        raise AlertsUnavailable("could not send the email right now; try again in a minute") from None
    except PermanentSinkError:
        raise ValidationFailed("the email provider refused this address") from None
    return expires


@dataclass(frozen=True)
class EmailVerifyResult:
    ok: bool
    email: Optional[str] = None
    previous_email: Optional[str] = None
    reason: Optional[str] = None           # no_code | expired | too_many_attempts | wrong_code
    attempts_left: int = 0


def verify_email_code(conn: Any, *, user_id: str, code: Any, now: datetime, pepper: bytes) -> EmailVerifyResult:
    """Check a code WITHOUT raising, so the caller can COMMIT the attempt counter before reporting a failure
    (raising inside the transaction would roll the counter back and allow brute force)."""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code.strip()):
        return EmailVerifyResult(False, reason="wrong_code")
    code = code.strip()
    row = _db.one(conn, """SELECT id, email, code_hash, attempts, expires_at > CAST(:now AS timestamptz) AS live
                            FROM email_verification_codes
                           WHERE user_id = CAST(:u AS uuid) AND consumed_at IS NULL AND superseded_at IS NULL
                           ORDER BY created_at DESC LIMIT 1 FOR UPDATE""", u=user_id, now=now)
    if row is None:
        return EmailVerifyResult(False, reason="no_code")
    if not row.get("live"):
        return EmailVerifyResult(False, reason="expired")
    attempts = int(row["attempts"])
    if attempts >= EMAIL_CODE_MAX_ATTEMPTS:
        return EmailVerifyResult(False, reason="too_many_attempts")
    expected = str(row["code_hash"])
    given = _code_hash(pepper, user_id, str(row["email"]), code)
    if not hmac.compare_digest(expected, given):
        _db.rows(conn, """UPDATE email_verification_codes SET attempts = attempts + 1
                           WHERE id = CAST(:id AS uuid) RETURNING attempts""", id=str(row["id"]))
        left = EMAIL_CODE_MAX_ATTEMPTS - attempts - 1
        return EmailVerifyResult(False, reason="too_many_attempts" if left <= 0 else "wrong_code",
                                 attempts_left=max(0, left))
    _db.rows(conn, """UPDATE email_verification_codes SET consumed_at = CAST(:now AS timestamptz)
                       WHERE id = CAST(:id AS uuid) RETURNING id""", id=str(row["id"]), now=now)
    email = str(row["email"])
    prev = _set_verified_email(conn, user_id, email, now)
    return EmailVerifyResult(True, email=email, previous_email=prev)


# ============================================================================================ Telegram
class TelegramBlocked(PermanentSinkError):
    """Telegram refused delivery to this chat for good (bot blocked / user deactivated / chat not found)."""

    def __init__(self, message: str, reason: str = "blocked") -> None:
        super().__init__(message)
        self.reason = reason


class TelegramBotApi:
    """Bot API ``sendMessage`` to a USER chat. Plain text (no parse_mode → no markup injection); link previews
    off. The token is part of the URL: never log the URL or the response body."""

    MAX_LEN = 4096

    def __init__(self, bot_token: str, *, session: Any | None = None, timeout: float = 8.0,
                 api_base: str = "https://api.telegram.org") -> None:
        self.bot_token, self.timeout, self.api_base = bot_token, timeout, api_base.rstrip("/")
        self._session = session

    @property
    def configured(self) -> bool:
        return bool(self.bot_token)

    def send_message(self, chat_id: int, text: str) -> None:
        if not self.configured:
            raise PermanentSinkError("telegram: bot token not configured")
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 1] + "…"
        session = self._session or _default_session()
        try:
            resp = session.post(f"{self.api_base}/bot{self.bot_token}/sendMessage",
                                json={"chat_id": int(chat_id), "text": text, "disable_web_page_preview": True},
                                timeout=self.timeout)
        except Exception as e:  # network error / timeout
            raise TransientSinkError(f"telegram: {type(e).__name__}") from None
        status = int(getattr(resp, "status_code", 0) or 0)
        body: dict[str, Any] = {}
        try:
            j = resp.json()
            body = j if isinstance(j, dict) else {}
        except Exception:
            body = {}
        desc = str(body.get("description") or "").lower()
        if status == 200 and body.get("ok") is True:
            return
        if status == 403:
            raise TelegramBlocked("telegram: HTTP 403 (bot blocked or user deactivated)", "blocked")
        if status == 400 and ("chat not found" in desc or "user not found" in desc):
            raise TelegramBlocked("telegram: chat not found", "chat_not_found")
        if status == 429:
            retry = None
            try:
                retry = float((body.get("parameters") or {}).get("retry_after"))
            except (TypeError, ValueError):
                retry = None
            raise TransientSinkError("telegram: rate limited", retry_after=retry)
        if status >= 500 or status == 0:
            raise TransientSinkError(f"telegram: HTTP {status}")
        raise PermanentSinkError(f"telegram: HTTP {status}")


def mark_telegram_blocked(conn: Any, *, user_id: str, now: datetime, reason: str = "blocked") -> bool:
    """403 / chat-not-found on send → mark the link lapsed (chat id kept for re-activation) and queue a
    mandatory email alert. Returns True when this call changed the state. Works under the executor role
    (UPDATE telegram_blocked_at / telegram_block_reason / updated_at only)."""
    if reason not in ("blocked", "chat_not_found"):
        reason = "blocked"
    r = _db.one(conn, """UPDATE user_contacts SET telegram_blocked_at = CAST(:now AS timestamptz),
                                telegram_block_reason = :r, updated_at = CAST(:now AS timestamptz)
                          WHERE user_id = CAST(:u AS uuid) AND telegram_chat_id IS NOT NULL
                            AND telegram_blocked_at IS NULL
                          RETURNING user_id""", u=user_id, now=now, r=reason)
    if r is None:
        return False
    queue_unreachable_alert(conn, user_id=user_id, now=now, reason=reason)
    log.warning("telegram link lapsed", extra={"fields": {"user_id": user_id, "reason": reason}})
    return True


def queue_unreachable_alert(conn: Any, *, user_id: str, now: datetime, reason: str) -> None:
    pause = now + BLOCK_GRACE
    _db.rows(conn, """INSERT INTO alerts (user_id, severity, kind, payload, dedup_key, created_at)
                      VALUES (CAST(:u AS uuid), 'warn', 'telegram_unreachable', CAST(:p AS jsonb), :k,
                              CAST(:now AS timestamptz))
                      ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING RETURNING id""",
             u=user_id, now=now, k=f"telegram_unreachable:{user_id}:{now.isoformat()}",
             p=_db.jdump({"reason": reason, "pause_at": pause.strftime("%Y-%m-%d %H:%M UTC"), "grace_hours": 24}))


class NoUserEmailContacts:
    """``app.alerts.notifier.ContactDirectory`` for the generic Notifier (executor / settlement): returns no user
    address, so the Notifier only emails OPS. User Telegram/email is delivered by app.alerts.delivery.deliver_outbox
    from the `alerts` rows (email policy, mutes, confirmed address, idempotency) — giving the Notifier a user
    directory would email users twice and bypass the email volume policy."""

    def email_for(self, user_id: str, alert: Any) -> Optional[str]:
        return None


# ============================================================================================ factories
def email_provider(settings: Any, session: Any | None = None) -> Optional[EmailProvider]:
    key = getattr(settings, "email_provider_api_key", "") or ""
    if not key:
        return None
    return ResendProvider(key, getattr(settings, "email_from", "") or "alerts@aijalon.trade", session=session)


def telegram_api(settings: Any, session: Any | None = None) -> Optional[TelegramBotApi]:
    token = getattr(settings, "telegram_bot_token", "") or ""
    return TelegramBotApi(token, session=session) if token else None


def masked(email: Optional[str]) -> Optional[str]:
    return mask_email(email) if email else None
