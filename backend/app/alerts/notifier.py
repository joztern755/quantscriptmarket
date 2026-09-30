"""Notifier: in-app + email + Telegram (ops), routed by severity. SPEC §5.5.

Routing
  info      -> in-app
  warn      -> in-app + email to the user (ops email when the alert has no user)
  critical  -> in-app + email (user and ops) + Telegram ops chat + auto-pause hook for market alerts

Guarantees
  * ``Notifier.notify`` NEVER raises into the caller. Every failure is logged (redacted) and counted in metrics.
  * Dedupe per alert key inside a window (default 30 min). The auto-pause hook runs even when the
    alert is deduped: pausing is idempotent and safety must not depend on notification state.
  * Per-user email rate limit (critical alerts bypass it) and a global Telegram rate limit.
  * Retries with exponential backoff on transient sink errors (5xx, 429, network); no retry on 4xx.
  * Messages are plain text built from fixed templates. Values are sanitized: secrets/64-hex strings are
    redacted, wallet addresses are shortened to 0x1234…abcd, email addresses are masked.

Multi-instance note: the default dedupe/rate-limit stores are in-memory (per Cloud Run instance). In prod pass a
shared ``DedupeStore`` (Postgres row with ``INSERT … ON CONFLICT`` on key + timestamp) so instances agree.

Lifting an auto-pause is NOT done here: it requires maker-checker in the admin flags module (SPEC §4 system_flags).
"""
from __future__ import annotations

import re
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol

from app.logging import get_logger, redact
from app.money import fmt_usd

log = get_logger("app.alerts")

__all__ = [
    "Severity",
    "Alert",
    "RenderedAlert",
    "NotifyResult",
    "TransientSinkError",
    "PermanentSinkError",
    "AlertRepo",
    "FlagRepo",
    "ContactDirectory",
    "DedupeStore",
    "InMemoryDedupeStore",
    "Metrics",
    "InMemoryMetrics",
    "InAppSink",
    "TelegramSink",
    "EmailProvider",
    "ResendProvider",
    "SendGridProvider",
    "EmailSink",
    "Notifier",
    "TEMPLATES",
    "render",
    "coerce_alert",
    "mask_address",
    "mask_email",
    "sanitize_text",
]


# --------------------------------------------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------------------------------------------
class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    """An alert to deliver. ``user_id=None`` = ops/admin alert (stored with user_id NULL -> admin console).

    ``data`` holds template parameters. Keys ending in ``_micro`` must be ints and are rendered as USD.
    ``coin`` marks a market alert; a critical market alert triggers the auto-pause hook unless
    ``auto_pause=False`` (e.g. app.domain.alerts_rules marks which critical alerts pause the market).
    ``key`` is the dedupe key; default ``kind:user_id:coin``.
    """

    kind: str
    severity: Severity
    user_id: str | None = None
    coin: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    key: str | None = None
    auto_pause: bool | None = None  # None = default (critical + coin); False = never; True = when critical + coin

    @property
    def dedupe_key(self) -> str:
        return self.key or f"{self.kind}:{self.user_id or '-'}:{self.coin or '-'}"

    @property
    def wants_pause(self) -> bool:
        return Severity(self.severity) is Severity.CRITICAL and bool(self.coin) and self.auto_pause is not False


@dataclass(frozen=True)
class RenderedAlert:
    kind: str
    severity: Severity
    user_id: str | None
    coin: str | None
    title: str
    body: str

    def as_text(self) -> str:
        return f"[{self.severity.value.upper()}] {self.title}\n{self.body}"


@dataclass
class NotifyResult:
    deduped: bool = False
    delivered: dict[str, bool] = field(default_factory=dict)  # channel -> success
    skipped: dict[str, str] = field(default_factory=dict)     # channel -> reason
    paused: bool = False
    errors: list[str] = field(default_factory=list)


class TransientSinkError(Exception):
    """Retryable failure (network error, HTTP 5xx, 429). ``retry_after`` in seconds if the provider said so."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentSinkError(Exception):
    """Non-retryable failure (HTTP 4xx other than 429, bad config)."""


# --------------------------------------------------------------------------------------------------------------
# Ports (implemented by the DB / API layers)
# --------------------------------------------------------------------------------------------------------------
class AlertRepo(Protocol):
    def insert_alert(self, user_id: str | None, severity: str, kind: str, payload: dict) -> None: ...


class FlagRepo(Protocol):
    def set_market_paused(self, coin: str, reason: str) -> None: ...


class ContactDirectory(Protocol):
    def email_for(self, user_id: str, alert: Alert) -> str | None:
        """Return the address to email for this user/alert, or None (no email, not verified, opted out,
        or plan not entitled — SPEC §1 plans). The API layer owns that decision."""
        ...


class DedupeStore(Protocol):
    def seen_recently(self, key: str, now: float, window_s: float) -> bool:
        """Atomically: return True if key was recorded within window; otherwise record it at ``now`` and return False."""
        ...


class Metrics(Protocol):
    def incr(self, name: str, **tags: str) -> None: ...


class InMemoryDedupeStore:
    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen_recently(self, key: str, now: float, window_s: float) -> bool:
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < window_s:
                return True
            self._last[key] = now
            if len(self._last) > 50_000:  # bound memory
                cutoff = now - window_s
                self._last = {k: v for k, v in self._last.items() if v >= cutoff}
            return False


class InMemoryMetrics:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def incr(self, name: str, **tags: str) -> None:
        suffix = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        self.counts[f"{name}{{{suffix}}}" if suffix else name] += 1


class _SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            q = self._events[key]
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


# --------------------------------------------------------------------------------------------------------------
# Sanitizing + templates
# --------------------------------------------------------------------------------------------------------------
_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MAX_VALUE_LEN = 300


def mask_address(addr: str) -> str:
    """0x1234567890abcdef... -> 0x1234…abcd (never show full wallet addresses in alerts)."""
    a = addr.strip()
    if len(a) < 12:
        return a
    return f"{a[:6]}…{a[-4:]}"


def mask_email(email: str) -> str:
    return _EMAIL_RE.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", email)


def sanitize_text(text: str) -> str:
    """Redact secrets (app.logging.redact first, so 64-hex keys never survive), shorten addresses, mask emails,
    drop control characters, cap length."""
    t = redact(str(text))
    t = _ADDR_RE.sub(lambda m: mask_address(m.group(0)), t)
    t = mask_email(t)
    t = _CTRL_RE.sub("", t)
    if len(t) > _MAX_VALUE_LEN:
        t = t[: _MAX_VALUE_LEN - 1] + "…"
    return t


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, (str, Decimal)):
        try:
            d = Decimal(str(value))
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def _format_value(key: str, value: Any) -> str:
    """Render one template value. ``*_micro`` ints -> USD; ``*_bps`` numbers -> percent; lists joined;
    everything else sanitized (secrets redacted, addresses shortened, emails masked)."""
    if key.endswith("_micro") and isinstance(value, int) and not isinstance(value, bool):
        return fmt_usd(value)
    if key.endswith("_bps"):
        d = _num(value)
        if d is not None:
            return f"{(d / 100).quantize(Decimal('0.01'))}%"
    if isinstance(value, (list, tuple, set, frozenset)):
        return sanitize_text(", ".join(str(v) for v in value))
    if value is None:
        return "n/a"
    return sanitize_text(value)


class _MissingParam(KeyError):
    pass


class _StrictParams(dict):
    def __missing__(self, key: str) -> str:
        raise _MissingParam(key)


# kind -> (title, body). Plain text; only simple {name} fields. Keep user-facing wording neutral and secret-free.
# Keys match the payloads built in app/domain/alerts_rules.py, app/execution/* and app/payments/*. When a
# template field is missing from the payload the generic rendering is used instead (never a half-filled text).
TEMPLATES: dict[str, tuple[str, str]] = {
    # balance / billing (SPEC §1)
    "balance_low": ("Fee balance running low", "Your fee balance is {balance_micro} (about {pct}% of the estimated monthly need). Top up to keep strategies opening new positions."),
    "balance_empty": ("Fee balance empty", "Your fee balance is {balance_micro}. Subscriptions will switch to reduce-only (exits only) after the grace period."),
    "subscription_past_due": ("Subscription past due", "Subscription {subscription} on {strategy} could not be renewed from your fee balance. Top up within the grace period to avoid reduce-only mode."),
    "subscription_reduce_only": ("Subscription set to reduce-only", "Subscription {subscription} on {strategy} can no longer open new positions. Top up your fee balance to resume."),
    "plan_past_due": ("Plan renewal failed", "Your {plan} plan renewal of {due_micro} could not be paid (available {available_micro}). Top up your fee balance."),
    "plan_downgraded": ("Plan downgraded", "Your plan changed from {from} to {to} because the renewal could not be paid."),
    # payments
    "topup_credited": ("Top-up received", "{amount_micro} was added to your fee balance ({method})."),
    "topup_failed": ("Top-up failed", "Your {method} top-up did not complete. No money was added. You can try again."),
    "topup_held": ("Deposit held for review", "A deposit of {amount_micro} ({method}) is held for manual review: {reason}."),
    "stripe_refund": ("Top-up refunded", "A refund of {amount_micro} was issued for a card/wallet top-up; your fee balance was reduced by the same amount."),
    "stripe_refund_ops": ("Stripe refund posted", "Refund on {charge} for user {user}: debit {amount_micro} from fee balance. Check balance and subscriptions (may be negative -> reduce-only)."),
    "stripe_dispute": ("Payment disputed", "A payment for a top-up was disputed with your bank. {amount_micro} has been removed from your fee balance until the dispute is resolved."),
    "stripe_dispute_ops": ("Stripe dispute opened", "Dispute {dispute} ({reason}) on {charge} for user {user}: debit {amount_micro}. Respond in the Stripe Dashboard before the deadline."),
    "stripe_dispute_closed_ops": ("Stripe dispute closed", "Dispute {dispute} closed with status {status} for user {user}; amount {amount_micro}."),
    "payment_manual_review": ("Payment needs manual review", "Event {event} ({event_type}): {reason}."),
    # trading / risk (SPEC §5.4, §5.5; app/domain/alerts_rules.py)
    "mark_oracle_divergence": ("Mark/oracle divergence on {coin}", "Mark {mark_px} deviates {deviation_bps} from oracle {oracle_px} on {coin}."),
    "oi_spike": ("Open-interest spike on {coin}", "Open interest on {coin} grew {growth_bps} in 1h ({oi_1h_ago_micro} -> {oi_now_micro})."),
    "funding_spike": ("Funding spike on {coin}", "Funding on {coin} is {funding_rate_per_hour} per hour (threshold {threshold})."),
    "user_drawdown": ("Drawdown alert", "Subscription {subscription_id} is at {pnl_24h_micro} over 24h on an allocation of {allocation_micro}."),
    "reject_burst": ("Order rejections", "{rejects} orders were rejected for subscription {subscription_id}; the circuit breaker may pause it."),
    "order_rejections_burst": ("Order rejections", "{count} orders were rejected for subscription {subscription}; the circuit breaker may pause it."),
    "agent_revoked": ("Agent approval changed", "The trading agent approval for wallet {master_address} is no longer active on-chain. Trading for this wallet stops until it is reconnected."),
    "new_country_login": ("New sign-in location", "Your account was signed into from a new country ({country}). If this was not you, secure your Google/Apple account and contact support."),
    "mfa_reset": ("Two-factor authentication reset", "Two-factor authentication on your account was reset. If this was not you, contact support immediately."),
    "withdrawal_request": ("Withdrawal requested", "A withdrawal of {amount_micro} was requested (request {request_id}) and awaits approval."),
    "payout_request": ("Payout requested", "A payout of {amount_micro} was requested (request {request_id}) and awaits two approvals."),
    "reconciliation_mismatch": ("Reconciliation mismatch: {scope}", "{scope}: difference {diff_micro} between ledger and on-chain."),
    "stale_signal": ("Stale strategy signal", "Signals are stale; new entries are not being placed."),
    "kill_switch": ("Kill switch engaged", "{scope} paused: {reason}."),
    # ops (RUNBOOK §7, §13.3, §13.9)
    "creator_signal_untrusted_dex": ("Creator signal on an untrusted dex", "Strategy version {strategy_version_id} produced weights for {markets} (dex {dexes}) outside the active trusted-dex allowlist (allowlist loaded: {allowlist_loaded}); those markets got no signal. RUNBOOK §13.9."),
    "kyc_awaiting_admin": ("KYC awaiting admin confirmation", "User {user_id} passed the {provider} identity check; one admin must confirm it (Admin -> KYC decision)."),
    "kyc_approval_revoked": ("KYC approval revoked by provider", "User {user_id}: an approved KYC changed to {status} ({event}). Payouts are blocked; review the user."),
    "agent_keygen_failed": ("Agent key generation failed", "Agent {agent_id}: {reason}. No key was stored; requests are retried once fixed. RUNBOOK agent keys."),
    "topup_held_now_attributable": ("Held deposit now attributable", "Held transfer {hash} ({amount_micro}) was sent from a wallet now verified by user {user_id}. Not credited automatically: release it by maker-checker (RUNBOOK §13.3)."),
}


def _humanize(kind: str) -> str:
    return sanitize_text(kind.replace("_", " ").strip().capitalize() or "Alert")


def _generic(alert: Alert, params: Mapping[str, str]) -> tuple[str, str]:
    title = _humanize(alert.kind) + (f" ({sanitize_text(alert.coin)})" if alert.coin else "")
    items = [f"{sanitize_text(k)}: {v}" for k, v in params.items() if k not in ("kind",)]
    return title, "; ".join(items) if items else _humanize(alert.kind)


def render(alert: Alert) -> RenderedAlert:
    params = {str(k): _format_value(str(k), v) for k, v in dict(alert.data).items()}
    if alert.coin:
        params.setdefault("coin", sanitize_text(alert.coin))
    tmpl = TEMPLATES.get(alert.kind)
    title = body = None
    if tmpl is not None:
        try:
            strict = _StrictParams(params)
            title, body = tmpl[0].format_map(strict), tmpl[1].format_map(strict)
        except (_MissingParam, ValueError, IndexError, AttributeError):
            title = body = None
    if title is None or body is None:
        title, body = _generic(alert, params)
    # Values were sanitized individually; sanitize the whole once more (the length cap applies per value).
    body = _CTRL_RE.sub("", mask_email(_ADDR_RE.sub(lambda m: mask_address(m.group(0)), redact(body))))
    return RenderedAlert(alert.kind, alert.severity, alert.user_id, alert.coin, sanitize_text(title), body[:3000])


def coerce_alert(obj: Any) -> Alert:
    """Accept our ``Alert``, ``app.domain.alerts_rules.Alert`` (payload/market/auto_pause_market) or
    ``app.execution.ports.AlertEvent`` (payload/coin/dedup_key)."""
    if isinstance(obj, Alert):
        return obj
    kind = getattr(obj, "kind", None)
    sev = getattr(obj, "severity", None)
    if not kind or sev is None:
        raise TypeError("not an alert")
    payload = getattr(obj, "payload", None) or getattr(obj, "data", None) or {}
    coin = getattr(obj, "market", None) or getattr(obj, "coin", None)
    auto = getattr(obj, "auto_pause_market", None)
    key = getattr(obj, "key", None) or getattr(obj, "dedup_key", None)
    return Alert(kind=str(kind), severity=Severity(sev), user_id=getattr(obj, "user_id", None), coin=coin,
                 data=dict(payload), key=key, auto_pause=auto)


# --------------------------------------------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------------------------------------------
def _default_session():
    import requests  # local import: tests inject fakes, prod has requests installed

    return requests.Session()


def _post_json(session: Any, url: str, json_body: dict, headers: dict | None, timeout: float, what: str) -> Any:
    try:
        resp = session.post(url, json=json_body, headers=headers or {}, timeout=timeout)
    except Exception as e:  # network errors, timeouts
        raise TransientSinkError(f"{what}: {type(e).__name__}") from None
    status = getattr(resp, "status_code", 0)
    if status == 429:
        retry_after = None
        try:
            retry_after = float(resp.headers.get("Retry-After"))  # type: ignore[union-attr]
        except Exception:
            try:
                retry_after = float(resp.json().get("parameters", {}).get("retry_after"))
            except Exception:
                retry_after = None
        raise TransientSinkError(f"{what}: rate limited", retry_after=retry_after)
    if status >= 500:
        raise TransientSinkError(f"{what}: HTTP {status}")
    if status >= 400 or status == 0:
        raise PermanentSinkError(f"{what}: HTTP {status}")
    return resp


# --------------------------------------------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------------------------------------------
class InAppSink:
    """Writes the rendered alert to the ``alerts`` table through the repo port (SPEC §4 alerts)."""

    name = "in_app"

    def __init__(self, repo: AlertRepo) -> None:
        self.repo = repo

    def send(self, alert: RenderedAlert, dedupe_key: str) -> None:
        payload = {"title": alert.title, "body": alert.body, "coin": alert.coin, "key": sanitize_text(dedupe_key)}
        try:
            self.repo.insert_alert(alert.user_id, alert.severity.value, alert.kind, payload)
        except Exception as e:
            raise TransientSinkError(f"in_app: {type(e).__name__}") from None


class TelegramSink:
    """Bot API ``sendMessage`` to the ops chat. Plain text (no parse_mode -> no markup injection).
    The bot token is part of the URL: never log the URL."""

    name = "telegram"
    MAX_LEN = 4096

    def __init__(self, bot_token: str, chat_id: str, session: Any | None = None, timeout: float = 5.0,
                 api_base: str = "https://api.telegram.org") -> None:
        self.bot_token, self.chat_id, self.timeout = bot_token, chat_id, timeout
        self.api_base = api_base.rstrip("/")
        self._session = session

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send_text(self, text: str) -> None:
        if not self.configured:
            raise PermanentSinkError("telegram: not configured")
        session = self._session or _default_session()
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 1] + "…"
        body = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        resp = _post_json(session, f"{self.api_base}/bot{self.bot_token}/sendMessage", body, None, self.timeout, "telegram")
        try:
            ok = bool(resp.json().get("ok"))
        except Exception:
            ok = False
        if not ok:
            raise PermanentSinkError("telegram: ok=false")

    def send(self, alert: RenderedAlert, dedupe_key: str) -> None:
        who = f"user {alert.user_id}" if alert.user_id else "ops"
        self.send_text(f"{alert.as_text()}\n({who}; key {sanitize_text(dedupe_key)})")


class EmailProvider(Protocol):
    name: str

    def send(self, to: str, subject: str, text: str) -> None: ...


class ResendProvider:
    """Resend (https://resend.com) transactional email: POST /emails, Bearer API key. Recommended default."""

    name = "resend"

    def __init__(self, api_key: str, sender: str, session: Any | None = None, timeout: float = 10.0,
                 api_base: str = "https://api.resend.com") -> None:
        self.api_key, self.sender, self.timeout, self.api_base = api_key, sender, timeout, api_base.rstrip("/")
        self._session = session

    def send(self, to: str, subject: str, text: str) -> None:
        if not self.api_key:
            raise PermanentSinkError("resend: no api key")
        _post_json(self._session or _default_session(), f"{self.api_base}/emails",
                   {"from": self.sender, "to": [to], "subject": subject, "text": text},
                   {"Authorization": f"Bearer {self.api_key}"}, self.timeout, "resend")


class SendGridProvider:
    """SendGrid v3 mail/send. Alternative provider behind the same interface."""

    name = "sendgrid"

    def __init__(self, api_key: str, sender: str, session: Any | None = None, timeout: float = 10.0,
                 api_base: str = "https://api.sendgrid.com") -> None:
        self.api_key, self.sender, self.timeout, self.api_base = api_key, sender, timeout, api_base.rstrip("/")
        self._session = session

    def send(self, to: str, subject: str, text: str) -> None:
        if not self.api_key:
            raise PermanentSinkError("sendgrid: no api key")
        body = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": self.sender},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}],
            "tracking_settings": {"click_tracking": {"enable": False}, "open_tracking": {"enable": False}},
        }
        _post_json(self._session or _default_session(), f"{self.api_base}/v3/mail/send", body,
                   {"Authorization": f"Bearer {self.api_key}"}, self.timeout, "sendgrid")


class EmailSink:
    name = "email"
    FOOTER = "\n\n— aijalon.trade. This is an automated notice; we will never ask for your seed phrase or private key."

    def __init__(self, provider: EmailProvider) -> None:
        self.provider = provider

    def send_to(self, to: str, alert: RenderedAlert) -> None:
        self.provider.send(to, f"[aijalon] {alert.title}", alert.body + self.FOOTER)


# --------------------------------------------------------------------------------------------------------------
# Notifier
# --------------------------------------------------------------------------------------------------------------
class Notifier:
    def __init__(
        self,
        *,
        in_app: InAppSink | None = None,
        email: EmailSink | None = None,
        telegram: TelegramSink | None = None,
        contacts: ContactDirectory | None = None,
        ops_emails: Iterable[str] = (),
        flags: FlagRepo | None = None,
        dedupe_store: DedupeStore | None = None,
        metrics: Metrics | None = None,
        dedupe_window_s: float = 30 * 60,
        email_per_user_per_hour: int = 10,
        telegram_per_minute: int = 20,
        retry_attempts: int = 3,
        retry_base_delay_s: float = 0.5,
        retry_max_delay_s: float = 8.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.in_app, self.email, self.telegram = in_app, email, telegram
        self.contacts = contacts
        self.ops_emails = tuple(ops_emails)
        self.flags = flags
        self.dedupe = dedupe_store or InMemoryDedupeStore()
        self.metrics = metrics or InMemoryMetrics()
        self.dedupe_window_s = dedupe_window_s
        self._email_limiter = _SlidingWindowLimiter(email_per_user_per_hour, 3600)
        self._telegram_limiter = _SlidingWindowLimiter(telegram_per_minute, 60)
        self.retry_attempts, self.retry_base_delay_s, self.retry_max_delay_s = retry_attempts, retry_base_delay_s, retry_max_delay_s
        self.clock, self.sleep = clock, sleep

    # ---- public ------------------------------------------------------------------------------------------
    def notify(self, alert: Any) -> NotifyResult:
        """Deliver an alert (ours, a domain Alert or an execution AlertEvent). Never raises."""
        result = NotifyResult()
        try:
            self._notify(coerce_alert(alert), result)
        except Exception as e:  # last-resort guard: a bug here must not break the caller (e.g. a webhook)
            result.errors.append(f"notifier: {type(e).__name__}")
            self._metric("alerts.notifier_error", kind=alert.kind if isinstance(alert, Alert) else "?")
            log.exception("notifier failure")
        return result

    def notify_many(self, alerts: Iterable[Any]) -> list[NotifyResult]:
        return [self.notify(a) for a in alerts]

    def send(self, *, severity: str, kind: str, payload: Mapping[str, Any] | None = None, user_id: str | None = None,
             coin: str | None = None, key: str | None = None) -> NotifyResult:
        """Keyword form for the API's NotifierPort adapter. Never raises."""
        try:
            alert = Alert(kind=kind, severity=Severity(severity), user_id=user_id, coin=coin, data=dict(payload or {}), key=key)
        except Exception as e:
            self._metric("alerts.notifier_error", kind=str(kind)[:40])
            return NotifyResult(errors=[f"notifier: {type(e).__name__}"])
        return self.notify(alert)

    # ---- internals ---------------------------------------------------------------------------------------
    def _notify(self, alert: Alert, result: NotifyResult) -> None:
        sev = Severity(alert.severity)
        now = self.clock()
        self._metric("alerts.received", kind=alert.kind, severity=sev.value)

        # Safety first: the auto-pause runs regardless of dedupe/delivery.
        if alert.wants_pause:
            result.paused = self._auto_pause(alert, result)

        if self.dedupe.seen_recently(alert.dedupe_key, now, self.dedupe_window_s):
            result.deduped = True
            self._metric("alerts.deduped", kind=alert.kind)
            return

        rendered = render(alert)

        # in-app: always
        if self.in_app is not None:
            self._deliver("in_app", lambda: self.in_app.send(rendered, alert.dedupe_key), result)
        else:
            result.skipped["in_app"] = "no sink"

        # email
        if sev in (Severity.WARN, Severity.CRITICAL):
            recipients: list[tuple[str, str]] = []  # (channel label, address)
            if alert.user_id:
                addr = None
                if self.contacts is not None:
                    try:
                        addr = self.contacts.email_for(alert.user_id, alert)
                    except Exception as e:
                        result.errors.append(f"contacts: {type(e).__name__}")
                if addr:
                    recipients.append(("email_user", addr))
                else:
                    result.skipped["email_user"] = "no address"
            if not alert.user_id or sev is Severity.CRITICAL:
                recipients.extend(("email_ops", a) for a in self.ops_emails)
            if self.email is None:
                for ch, _ in recipients:
                    result.skipped[ch] = "no sink"
            else:
                for ch, addr in recipients:
                    if ch == "email_user" and sev is not Severity.CRITICAL and not self._email_limiter.allow(alert.user_id or "", now):
                        result.skipped[ch] = "rate limited"
                        self._metric("alerts.rate_limited", channel=ch)
                        continue
                    self._deliver(ch, lambda a=addr: self.email.send_to(a, rendered), result)

        # telegram (ops page)
        if sev is Severity.CRITICAL:
            if self.telegram is None or not self.telegram.configured:
                result.skipped["telegram"] = "no sink"
                self._metric("alerts.telegram_unconfigured")
            elif not self._telegram_limiter.allow("ops", now):
                result.skipped["telegram"] = "rate limited"
                self._metric("alerts.rate_limited", channel="telegram")
            else:
                self._deliver("telegram", lambda: self.telegram.send(rendered, alert.dedupe_key), result)

    def _auto_pause(self, alert: Alert, result: NotifyResult) -> bool:
        if self.flags is None:
            result.errors.append("auto_pause: no flag repo")
            self._metric("alerts.auto_pause_failed", reason="no_repo")
            log.error("auto-pause requested but no FlagRepo configured", extra={"fields": {"kind": alert.kind, "coin": alert.coin}})
            return False
        reason = sanitize_text(f"auto: critical alert {alert.kind}")
        try:
            self.flags.set_market_paused(alert.coin or "", reason)
        except Exception as e:
            result.errors.append(f"auto_pause: {type(e).__name__}")
            self._metric("alerts.auto_pause_failed", reason=type(e).__name__)
            log.error("auto-pause failed", extra={"fields": {"kind": alert.kind, "coin": alert.coin, "error": type(e).__name__}})
            return False
        self._metric("alerts.auto_paused", coin=alert.coin or "")
        log.warning("market auto-paused", extra={"fields": {"kind": alert.kind, "coin": alert.coin}})
        return True

    def _deliver(self, channel: str, fn: Callable[[], None], result: NotifyResult) -> None:
        attempt = 0
        while True:
            attempt += 1
            try:
                fn()
                result.delivered[channel] = True
                self._metric("alerts.sent", channel=channel)
                return
            except TransientSinkError as e:
                if attempt >= self.retry_attempts:
                    self._fail(channel, str(e), result)
                    return
                delay = min(self.retry_max_delay_s, self.retry_base_delay_s * (2 ** (attempt - 1)))
                if e.retry_after is not None:
                    delay = min(self.retry_max_delay_s, max(delay, e.retry_after))
                self._metric("alerts.retry", channel=channel)
                self.sleep(delay)
            except PermanentSinkError as e:
                self._fail(channel, str(e), result)
                return
            except Exception as e:
                self._fail(channel, f"{channel}: {type(e).__name__}", result)
                return

    def _fail(self, channel: str, msg: str, result: NotifyResult) -> None:
        result.delivered[channel] = False
        result.errors.append(sanitize_text(msg))
        self._metric("alerts.failed", channel=channel)
        log.warning("alert delivery failed", extra={"fields": {"channel": channel, "error": sanitize_text(msg)}})

    def _metric(self, name: str, **tags: str) -> None:
        try:
            self.metrics.incr(name, **tags)
        except Exception:
            pass
