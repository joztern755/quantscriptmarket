"""User-alert delivery worker + producers that live with it (SPEC §12 user alerts, email volume policy).

Jobs (mounted on the EXECUTOR service, Cloud Scheduler OIDC, DB role app_executor):
  POST /v1/internal/deliver-alerts     every minute  → :func:`deliver_outbox`
  POST /v1/internal/daily-pnl-summary  00:15 UTC     → :func:`emit_daily_pnl_summaries`

deliver_outbox(db, now) — one pass, idempotent, safe to run concurrently:
  1. materialise events_outbox (0006: id, user_id, kind, payload, created_at, delivered_at) into `alerts` rows
     (dedup_key 'outbox:<id>', so the in-app list shows them) and stamp delivered_at — skipped while the table
     does not exist;
  2. fan out ops alerts that affect users: kill switch / new-entries pause (kill_switch_engaged), critical
     auto-pause market alerts (mark_oracle_divergence, oi_spike, funding_spike) → `market_paused` for every
     user with a live subscription on a strategy trading that coin (global switch → every live subscriber);
     stale_signal → `signal_stale` for that version's subscribers (idempotent via dedup_key 'fanout:<alert>:<user>'
     and an alert_deliveries(channel='fanout') marker);
  3. deliver every recent USER alert (48 h window) to Telegram and email according to app.alerts.prefs.route +
     the user's mutes + contacts. Each (alert, channel) is claimed in alert_deliveries (UNIQUE(alert_id, channel))
     with a 5-minute lease, then finalised as sent / retry (backoff 1, 2, 5, 15, 30 min; 6 attempts) / failed /
     skipped (reason in last_error: policy, muted, no_telegram, no_email, blocked…). Telegram 403 / chat not found
     → the link is marked lapsed (user_sinks.mark_telegram_blocked), a mandatory `telegram_unreachable` alert is
     queued (email only) and the executor pauses new entries after 24 h (alert_contacts_entries_allowed).

Low-balance hook: :func:`on_balance_changed` — call it whenever a user's fee balance changes, in the SAME
transaction, with the balance before and after (user-facing, i.e. −ledger balance):
  * API: wired in app/api/ledger_ops.post (every fee-balance posting made by the API).
  * Settlement / renewals / deposit scans that post outside ledger_ops MUST call
    ``on_balance_changed(conn, user_id, prev_micro, new_micro)`` after each posting that touches
    ``user:{id}:fee_balance`` (profit share, subscription renewal, plan renewal, deposits credited by jobs).
It emits balance_low (50 %, 20 %) / balance_empty (0 %) alerts when a threshold of the estimated monthly need
(Σ live subscription prices + plan price) is crossed downwards (app.domain.billing.crossed_low_balance_thresholds).

Producers of other user kinds (payload contract in app/alerts/user_templates.py) write `alerts` rows with a
user_id (or events_outbox rows); this worker is the only thing that sends user Telegram/email. The generic
Notifier must NOT be given a user ContactDirectory (ops routing only) or users would get duplicate emails.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.domain.alerts_rules import AUTO_PAUSE_KINDS
from app.domain.billing import crossed_low_balance_thresholds, estimate_monthly_need
from app.logging import get_logger

from . import _db
from .notifier import EmailProvider, EmailSink, PermanentSinkError, RenderedAlert, Severity, TransientSinkError, sanitize_text
from .prefs import muted_kinds, route
from .user_sinks import (
    TelegramBlocked,
    TelegramBotApi,
    email_provider,
    mark_telegram_blocked,
    telegram_api,
)
from .user_templates import render_user_alert, telegram_text

log = get_logger("app.alerts.delivery")

__all__ = [
    "deliver_outbox", "DeliveryReport", "on_balance_changed", "low_balance_alerts", "emit_daily_pnl_summaries",
    "send_test_alert", "LOOKBACK", "BACKOFF", "LIVE_STATUSES",
]

LOOKBACK = timedelta(hours=48)
LEASE = timedelta(minutes=5)
BACKOFF = (timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=15),
           timedelta(minutes=30))
MAX_ATTEMPTS = len(BACKOFF) + 1
LIVE_STATUSES = ["pending", "active", "past_due", "reduce_only", "paused_user", "closing"]
_CRITICAL_OUTBOX = ["agent_expired", "builder_approval_missing", "balance_empty", "subscription_reduce_only",
                    "market_paused"]
_WARN_OUTBOX = ["agent_expiring", "balance_low", "subscription_past_due", "user_drawdown", "signal_stale",
                "strategy_paused", "stripe_refund", "stripe_dispute", "withdrawal_requested", "new_device_login",
                "mfa_changed"]
_FLAG_GLOBAL = ("kill_switch_global", "new_entries_paused")


@dataclass
class DeliveryReport:
    materialized: int = 0
    fanned_out: int = 0
    sent: dict[str, int] = field(default_factory=lambda: {"telegram": 0, "email": 0})
    skipped: int = 0
    retry: int = 0
    failed: int = 0
    blocked_users: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"materialized": self.materialized, "fanned_out": self.fanned_out,
                "sent_telegram": self.sent["telegram"], "sent_email": self.sent["email"], "skipped": self.skipped,
                "retry": self.retry, "failed": self.failed, "blocked_users": self.blocked_users}


# ============================================================================================ 1. outbox
def _outbox_exists(conn: Any) -> bool:
    r = _db.one(conn, "SELECT to_regclass('public.events_outbox') IS NOT NULL AS ok")
    return bool(r and r.get("ok"))


def materialize_outbox(conn: Any, now: datetime, limit: int = 500) -> int:
    """events_outbox (0006) → `alerts` rows, then stamp delivered_at (same statement → atomic).
    * user events keep their kind; the strategy name is added from payload.strategy_id;
    * trade_closed / trade_resized with a non-zero realized PnL also yield a `trade_pnl` alert (so "realized
      profit / loss per closed trade" can be muted independently of the trade alerts);
    * ops events (user_id NULL) land in the admin console; warn/critical ones are paged by this worker.
    A severity of 'info' on a kind that is warn/critical by policy is upgraded."""
    if not _outbox_exists(conn):
        return 0
    rows = _db.rows(conn, """
        WITH ev AS (
            SELECT e.id, e.user_id, e.kind, e.severity, e.payload FROM events_outbox e
             WHERE e.delivered_at IS NULL ORDER BY e.id LIMIT :lim FOR UPDATE SKIP LOCKED
        ), src AS (
            SELECT ev.id, ev.user_id, ev.kind,
                   (CASE WHEN ev.severity <> 'info' THEN ev.severity::text
                         WHEN ev.kind = ANY(CAST(:crit AS text[])) THEN 'critical'
                         WHEN ev.kind = ANY(CAST(:warn AS text[])) THEN 'warn'
                         ELSE 'info' END) AS sev,
                   (CASE WHEN jsonb_typeof(ev.payload) = 'object' THEN ev.payload ELSE '{}'::jsonb END)
                     || jsonb_strip_nulls(jsonb_build_object('strategy', st.name))
                     || jsonb_build_object('_source', 'outbox', '_event_id', ev.id) AS p
              FROM ev LEFT JOIN strategies st ON st.id::text = ev.payload->>'strategy_id'
             WHERE ev.kind ~ '^[a-z][a-z0-9_]{1,63}$'
        ), ins AS (
            INSERT INTO alerts (user_id, severity, kind, payload, dedup_key, created_at)
            SELECT user_id, CAST(sev AS alert_severity), kind, p, 'outbox:' || id::text, CAST(:now AS timestamptz)
              FROM src
            UNION ALL
            SELECT user_id, CAST('info' AS alert_severity), 'trade_pnl', p, 'outbox:' || id::text || '/pnl',
                   CAST(:now AS timestamptz)
              FROM src
             WHERE user_id IS NOT NULL AND kind IN ('trade_closed', 'trade_resized')
               AND coalesce(p->>'net_pnl_micro', p->>'realized_pnl_micro') ~ '^-?[0-9]{1,18}$'
               AND CAST(coalesce(p->>'net_pnl_micro', p->>'realized_pnl_micro') AS bigint) <> 0
            ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
            RETURNING id
        ), upd AS (
            UPDATE events_outbox e SET delivered_at = CAST(:now AS timestamptz)
              FROM ev WHERE e.id = ev.id
            RETURNING e.id
        )
        SELECT id FROM upd""", lim=limit, now=now, crit=_CRITICAL_OUTBOX, warn=_WARN_OUTBOX)
    return len(rows)


# ============================================================================================ 2. fan-out
def _flag_scope(key: str) -> tuple[Optional[str], bool]:
    """system_flags key → (coin, is_global). kill_switch_market:xyz:SILVER → ('xyz:SILVER', False)."""
    if key in _FLAG_GLOBAL:
        return None, True
    for prefix in ("kill_switch_market:", "new_entries_paused:"):
        if key.startswith(prefix) and len(key) > len(prefix):
            return key[len(prefix):], False
    return None, False


def fanout_ops_alerts(conn: Any, now: datetime, limit: int = 50) -> int:
    ops = _db.rows(conn, """
        SELECT a.id, a.kind, a.severity::text AS severity, a.payload FROM alerts a
         WHERE a.user_id IS NULL AND a.created_at >= CAST(:since AS timestamptz)
           AND (a.kind IN ('kill_switch_engaged', 'stale_signal')
                OR (a.severity = 'critical' AND a.kind = ANY(CAST(:auto AS text[]))))
           AND NOT EXISTS (SELECT 1 FROM alert_deliveries d WHERE d.alert_id = a.id AND d.channel = 'fanout')
         ORDER BY a.created_at, a.id LIMIT :lim""", since=now - LOOKBACK, auto=sorted(AUTO_PAUSE_KINDS), lim=limit)
    total = 0
    for a in ops:
        payload = _db.jload(a.get("payload"))
        aid = str(a["id"])
        kind, sev, new_payload, coin, is_global, version = None, "critical", {}, None, False, None
        if a["kind"] == "kill_switch_engaged":
            coin, is_global = _flag_scope(str(payload.get("key") or ""))
            if coin or is_global:
                kind = "market_paused"
                new_payload = {"scope": coin or "all markets",
                               "cause": "kill switch" if "kill_switch" in str(payload.get("key")) else "entries paused"}
        elif a["kind"] == "stale_signal":
            version = payload.get("strategy_version_id")
            if isinstance(version, str) and len(version) == 36:
                kind, sev = "signal_stale", "warn"
        else:
            c = payload.get("coin")
            if isinstance(c, str) and c:
                coin, kind = c, "market_paused"
                new_payload = {"scope": c, "cause": str(a["kind"]).replace("_", " ")}
        n = 0
        if kind is not None:
            n = len(_db.rows(conn, """
                INSERT INTO alerts (user_id, severity, kind, payload, dedup_key, created_at)
                SELECT DISTINCT ON (s.user_id) s.user_id, CAST(:sev AS alert_severity), :kind,
                       CAST(:p AS jsonb) || jsonb_build_object('strategy', st.name, '_source', 'fanout'),
                       'fanout:' || :aid || ':' || s.user_id::text, CAST(:now AS timestamptz)
                  FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
                 WHERE s.status::text = ANY(CAST(:live AS text[]))
                   AND (CAST(:glob AS boolean)
                        OR (CAST(:coin AS text) IS NOT NULL AND CAST(:coin AS text) = ANY(st.markets))
                        OR (CAST(:ver AS text) IS NOT NULL AND s.strategy_version_id::text = CAST(:ver AS text)))
                 ORDER BY s.user_id, st.name
                ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
                RETURNING id""", sev=sev, kind=kind, p=_db.jdump(new_payload), aid=aid, now=now, live=LIVE_STATUSES,
                glob=is_global, coin=coin, ver=version))
        _db.rows(conn, """INSERT INTO alert_deliveries (alert_id, channel, status, attempts, sent_at, updated_at, last_error)
                          VALUES (CAST(:a AS uuid), 'fanout', 'sent', 1, CAST(:now AS timestamptz),
                                  CAST(:now AS timestamptz), :note)
                          ON CONFLICT (alert_id, channel) DO NOTHING RETURNING id""",
                 a=aid, now=now, note=f"users={n}")
        total += n
    return total


# ============================================================================================ 3. deliver
class _Contacts:
    def __init__(self, rows: list[dict]) -> None:
        self.by_user = {str(r["user_id"]): r for r in rows}

    def chat(self, uid: str) -> Optional[int]:
        r = self.by_user.get(uid)
        if not r or r.get("telegram_chat_id") is None or r.get("telegram_blocked_at") is not None:
            return None
        return int(r["telegram_chat_id"])

    def email(self, uid: str) -> Optional[str]:
        r = self.by_user.get(uid)
        if not r or not r.get("email") or r.get("email_verified_at") is None:
            return None
        return str(r["email"])

    def block(self, uid: str) -> None:
        r = self.by_user.get(uid)
        if r is not None:
            r["telegram_blocked_at"] = "now"


def _claim(conn: Any, alert_id: str, channel: str, now: datetime) -> Optional[int]:
    r = _db.one(conn, """
        INSERT INTO alert_deliveries (alert_id, channel, status, attempts, next_attempt_at, updated_at)
        VALUES (CAST(:a AS uuid), :c, 'sending', 1, CAST(:lease AS timestamptz), CAST(:now AS timestamptz))
        ON CONFLICT (alert_id, channel) DO UPDATE
           SET status = 'sending', attempts = alert_deliveries.attempts + 1, next_attempt_at = EXCLUDED.next_attempt_at,
               updated_at = EXCLUDED.updated_at
         WHERE alert_deliveries.status IN ('retry', 'sending')
           AND alert_deliveries.next_attempt_at <= CAST(:now AS timestamptz)
        RETURNING attempts""", a=alert_id, c=channel, lease=now + LEASE, now=now)
    return int(r["attempts"]) if r else None


def _finish(conn: Any, alert_id: str, channel: str, status: str, now: datetime, *, error: Optional[str] = None,
            next_at: Optional[datetime] = None) -> None:
    _db.rows(conn, """
        UPDATE alert_deliveries SET status = :s, updated_at = CAST(:now AS timestamptz),
               next_attempt_at = CAST(:nx AS timestamptz),
               sent_at = CASE WHEN :s = 'sent' THEN CAST(:now AS timestamptz) ELSE NULL END,
               last_error = CAST(:err AS text)
         WHERE alert_id = CAST(:a AS uuid) AND channel = :c AND status = 'sending'
        RETURNING id""", s=status, now=now, nx=next_at, err=(sanitize_text(error)[:300] if error else None),
             a=alert_id, c=channel)


def _skip(conn: Any, alert_id: str, channel: str, reason: str, now: datetime) -> bool:
    r = _db.one(conn, """
        INSERT INTO alert_deliveries (alert_id, channel, status, attempts, last_error, updated_at)
        VALUES (CAST(:a AS uuid), :c, 'skipped', 0, :r, CAST(:now AS timestamptz))
        ON CONFLICT (alert_id, channel) DO UPDATE
           SET status = 'skipped', last_error = EXCLUDED.last_error, next_attempt_at = NULL,
               updated_at = EXCLUDED.updated_at
         WHERE alert_deliveries.status IN ('retry', 'sending')
           AND alert_deliveries.next_attempt_at <= CAST(:now AS timestamptz)
        RETURNING id""", a=alert_id, c=channel, r=reason, now=now)
    return r is not None


def _candidates(conn: Any, now: datetime, limit: int) -> list[dict]:
    return _db.rows(conn, """
        SELECT a.id, a.user_id, a.kind, a.severity::text AS severity, a.payload, a.created_at FROM alerts a
         WHERE a.created_at >= CAST(:since AS timestamptz)
           AND (a.user_id IS NOT NULL                                  -- user alerts
                OR (a.dedup_key LIKE 'outbox:%' AND a.severity IN ('warn', 'critical')))   -- ops events from 0006
           AND EXISTS (
               SELECT 1 FROM (VALUES ('telegram'), ('email')) ch(c)
                WHERE NOT EXISTS (
                    SELECT 1 FROM alert_deliveries d
                     WHERE d.alert_id = a.id AND d.channel = ch.c
                       AND (d.status IN ('sent', 'failed', 'skipped') OR d.next_attempt_at > CAST(:now AS timestamptz))))
         ORDER BY a.created_at, a.id LIMIT :lim""", since=now - LOOKBACK, now=now, lim=limit)


def _load_contacts(conn: Any, uids: list[str]) -> _Contacts:
    if not uids:
        return _Contacts([])
    return _Contacts(_db.rows(conn, """
        SELECT user_id, telegram_chat_id, telegram_blocked_at, email, email_verified_at FROM user_contacts
         WHERE user_id = ANY(CAST(:ids AS uuid[]))""", ids=sorted(set(uids))))


def _existing(conn: Any, alert_ids: list[str]) -> dict[tuple[str, str], str]:
    if not alert_ids:
        return {}
    return {(str(r["alert_id"]), str(r["channel"])): str(r["status"]) for r in _db.rows(conn, """
        SELECT alert_id, channel, status FROM alert_deliveries WHERE alert_id = ANY(CAST(:ids AS uuid[]))""",
        ids=alert_ids)}


def _ops_chat(settings: Any) -> Optional[int]:
    raw = str(getattr(settings, "telegram_ops_chat_id", "") or "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def deliver_outbox(db: Any, now: Optional[datetime] = None, *, settings: Any = None,
                   telegram: Optional[TelegramBotApi] = None, email: Optional[EmailProvider] = None,
                   batch: int = 200, time_budget_s: float = 45.0,
                   clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One delivery pass (see module doc). ``db`` is a DatabasePort (``begin()`` → transactional connection).
    Sinks default to the configured Telegram bot / Resend provider from ``settings``."""
    now = now or datetime.now(timezone.utc)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    telegram = telegram if telegram is not None else telegram_api(settings)
    email = email if email is not None else email_provider(settings)
    origin = getattr(settings, "web_origin", "") or "https://aijalon.trade"
    rep = DeliveryReport()
    started = clock()

    with db.begin() as conn:
        rep.materialized = materialize_outbox(conn, now)
    with db.begin() as conn:
        rep.fanned_out = fanout_ops_alerts(conn, now)
    with db.begin() as conn:
        cands = _candidates(conn, now, batch)
        uids = [str(c["user_id"]) for c in cands if c.get("user_id") is not None]
        contacts = _load_contacts(conn, uids)
        mutes = muted_kinds(conn, uids)
        done = _existing(conn, [str(c["id"]) for c in cands])

    ops_chat = _ops_chat(settings)
    ops_emails = [e for e in (getattr(settings, "ops_emails", ()) or ()) if e]
    for a in cands:
        if clock() - started > time_budget_s:
            break
        aid, kind, sev = str(a["id"]), str(a["kind"]), str(a["severity"])
        is_ops = a.get("user_id") is None
        uid = "" if is_ops else str(a["user_id"])
        if is_ops:   # ops events from the outbox: critical → ops Telegram chat; warn/critical → ops emails
            want_tg, want_email, mandatory, muted = sev == "critical", True, True, False
            tg_target: Optional[int] = ops_chat
            mail_to = ops_emails
        else:
            want_tg, want_email, mandatory = route(kind, sev)
            muted = (not mandatory) and kind in mutes.get(uid, set())
            tg_target = contacts.chat(uid)
            addr = contacts.email(uid)
            mail_to = [addr] if addr else []
        payload = _db.jload(a.get("payload"))
        title, body = render_user_alert(kind, sev, payload, web_origin=origin)
        if is_ops:
            title = f"[ops] {title}"
        for channel, wanted in (("telegram", want_tg), ("email", want_email)):
            if done.get((aid, channel)) in ("sent", "failed", "skipped"):
                continue
            if channel == "telegram" and not is_ops:
                tg_target = contacts.chat(uid)           # may have lapsed earlier in this pass
            with db.begin() as conn:
                reason = None
                if not wanted:
                    reason = "policy"
                elif muted:
                    reason = "muted"
                elif channel == "telegram" and (telegram is None or not telegram.configured):
                    reason = "telegram_not_configured"
                elif channel == "telegram" and tg_target is None:
                    reason = "no_telegram"
                elif channel == "email" and email is None:
                    reason = "email_not_configured"
                elif channel == "email" and not mail_to:
                    reason = "no_email"
                if reason is not None:
                    if _skip(conn, aid, channel, reason, now):
                        rep.skipped += 1
                    continue
                attempts = _claim(conn, aid, channel, now)
            if attempts is None:
                continue                                    # another worker holds it, or backoff not due
            status, err, next_at = "sent", None, None
            try:
                if channel == "telegram":
                    assert telegram is not None and tg_target is not None
                    telegram.send_message(tg_target, telegram_text(title, body, sev))
                else:
                    assert email is not None
                    for to in mail_to:
                        EmailSink(email).send_to(to, RenderedAlert(kind, Severity(sev), uid or None, None, title, body))
            except TelegramBlocked as e:
                status, err = "failed", f"blocked: {e.reason}"
                if not is_ops:
                    with db.begin() as conn:
                        if mark_telegram_blocked(conn, user_id=uid, now=now, reason=e.reason):
                            rep.blocked_users += 1
                    contacts.block(uid)
            except TransientSinkError as e:
                if attempts >= MAX_ATTEMPTS:
                    status, err = "failed", f"gave up: {e}"
                else:
                    delay = BACKOFF[min(attempts, len(BACKOFF)) - 1]
                    if e.retry_after:
                        delay = max(delay, timedelta(seconds=min(float(e.retry_after), 3600)))
                    status, err, next_at = "retry", str(e), now + delay
            except PermanentSinkError as e:
                status, err = "failed", str(e)
            except Exception as e:  # noqa: BLE001 - a sink bug must not stop the pass
                status, err = "failed", f"{channel}: {type(e).__name__}"
            with db.begin() as conn:
                _finish(conn, aid, channel, status, now, error=err, next_at=next_at)
            if status == "sent":
                rep.sent[channel] += 1
            elif status == "retry":
                rep.retry += 1
            else:
                rep.failed += 1
                log.warning("user alert delivery failed", extra={"fields": {"channel": channel, "kind": kind,
                                                                            "error": sanitize_text(err or "")}})
    return rep.as_dict()


# ============================================================================================ test alert
def send_test_alert(conn: Any, *, user_id: str, now: datetime, settings: Any,
                    telegram: Optional[TelegramBotApi] = None, email: Optional[EmailProvider] = None) -> dict[str, str]:
    """Synchronous test to both channels (the API's POST /v1/alerts/test). Returns {channel: result} where result
    is sent | not_linked | not_verified | not_configured | blocked | failed | retry_later. A 403 marks the link
    lapsed exactly like the worker does. Also records an in-app `test_alert` row."""
    telegram = telegram if telegram is not None else telegram_api(settings)
    email = email if email is not None else email_provider(settings)
    origin = getattr(settings, "web_origin", "") or "https://aijalon.trade"
    c = _load_contacts(conn, [user_id])
    title, body = render_user_alert("test_alert", "info", {}, web_origin=origin)
    out: dict[str, str] = {}
    chat = c.chat(user_id)
    if telegram is None or not telegram.configured:
        out["telegram"] = "not_configured"
    elif chat is None:
        out["telegram"] = "not_linked"
    else:
        try:
            telegram.send_message(chat, telegram_text(title, body, "info"))
            out["telegram"] = "sent"
        except TelegramBlocked as e:
            mark_telegram_blocked(conn, user_id=user_id, now=now, reason=e.reason)
            out["telegram"] = "blocked"
        except TransientSinkError:
            out["telegram"] = "retry_later"
        except Exception:  # noqa: BLE001
            out["telegram"] = "failed"
    addr = c.email(user_id)
    if email is None:
        out["email"] = "not_configured"
    elif addr is None:
        out["email"] = "not_verified"
    else:
        try:
            EmailSink(email).send_to(addr, RenderedAlert("test_alert", Severity.INFO, user_id, None, title, body))
            out["email"] = "sent"
        except TransientSinkError:
            out["email"] = "retry_later"
        except Exception:  # noqa: BLE001
            out["email"] = "failed"
    # in-app record; prefs.route("test_alert") has no worker channels, so it is never re-sent
    _db.rows(conn, """INSERT INTO alerts (user_id, severity, kind, payload, created_at)
                      VALUES (CAST(:u AS uuid), 'info', 'test_alert', CAST(:p AS jsonb), CAST(:now AS timestamptz))
                      RETURNING id""", u=user_id, now=now, p=_db.jdump({"results": out}))
    return out


# ============================================================================================ low balance
def low_balance_alerts(user_id: str, prev_micro: Optional[int], new_micro: int, need_micro: int) -> list[dict[str, Any]]:
    """Pure: alerts for thresholds newly crossed downwards (50 %, 20 % → balance_low; 0 % → balance_empty)."""
    out = []
    for t in crossed_low_balance_thresholds(prev_micro, new_micro, need_micro):
        out.append({
            "kind": "balance_empty" if t == 0 else "balance_low",
            "severity": "critical" if t == 0 else "warn",
            "payload": {"balance_micro": int(new_micro), "threshold_bps": int(t), "need_micro": int(need_micro)},
            "dedup_key": f"balance:{user_id}:{t}:{prev_micro}:{new_micro}",
        })
    return out


def _need_sql(conn: Any, user_id: str) -> int:
    from app.config import Economics
    r = _db.one(conn, """SELECT u.plan::text AS plan,
                                (SELECT coalesce(sum(coalesce(st.price_monthly_micro, 0)), 0)::bigint
                                   FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
                                  WHERE s.user_id = u.id
                                    AND s.status::text IN ('pending', 'active', 'past_due', 'reduce_only')) AS subs
                           FROM users u WHERE u.id = CAST(:u AS uuid)""", u=user_id)
    if not r:
        return 0
    try:
        plan_price = Economics().plan(str(r["plan"])).price_monthly_micro
    except KeyError:
        plan_price = 0
    return estimate_monthly_need([int(r["subs"] or 0)], plan_price)


def on_balance_changed(db: Any, user_id: str, prev: Optional[int], new: int, *, svc: Any = None,
                       now: Optional[datetime] = None, raise_errors: bool = False) -> list[str]:
    """Emit low-balance alerts for a fee-balance change (see module doc). ``db`` is the caller's open transactional
    connection. With ``svc`` (the API's Services) the ports are used (works with the API test fakes); without it
    plain SQL is used (settlement / executor). Returns the emitted kinds. Never raises unless ``raise_errors``
    (use that inside a SAVEPOINT so a failed statement cannot poison the caller's transaction)."""
    try:
        if prev is not None and int(prev) == int(new):
            return []
        if svc is not None:
            user = svc.store.get_user(db, user_id) or {}
            need = svc.domain.estimate_monthly_need(svc.store.live_subscription_prices(db, user_id),
                                                    svc.domain.plan_price(str(user.get("plan") or "free")))
        else:
            need = _need_sql(db, user_id)
        specs = low_balance_alerts(user_id, prev, int(new), int(need))
        for s in specs:
            if svc is not None:
                svc.notifier.notify(db, user_id=user_id, severity=s["severity"], kind=s["kind"], payload=s["payload"])
            else:
                _db.rows(db, """INSERT INTO alerts (user_id, severity, kind, payload, dedup_key, created_at)
                                VALUES (CAST(:u AS uuid), CAST(:s AS alert_severity), :k, CAST(:p AS jsonb), :d,
                                        CAST(:now AS timestamptz))
                                ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING RETURNING id""",
                         u=user_id, s=s["severity"], k=s["kind"], p=_db.jdump(s["payload"]), d=s["dedup_key"],
                         now=now or datetime.now(timezone.utc))
        return [s["kind"] for s in specs]
    except Exception as e:  # noqa: BLE001 - alerts must never break a money movement
        if raise_errors:
            raise
        log.warning("low-balance hook failed", extra={"fields": {"user_id": user_id, "error": type(e).__name__}})
        return []


# ============================================================================================ daily PnL
def emit_daily_pnl_summaries(db: Any, now: Optional[datetime] = None, *, day: Optional[date] = None) -> dict[str, Any]:
    """00:15 UTC job: one `daily_pnl_summary` alert per user with at least one attributed fill on the previous UTC
    day. Realized PnL = Σ(closed_pnl − fee) over our fills (funding excluded). Idempotent (dedup_key
    'daily_pnl:<user>:<date>')."""
    now = now or datetime.now(timezone.utc)
    d = day or (now - timedelta(days=1)).date()
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    with db.begin() as conn:
        per = _db.rows(conn, """
            SELECT s.user_id, st.name AS strategy,
                   sum(f.closed_pnl_micro - f.fee_micro)::bigint AS pnl, sum(f.fee_micro)::bigint AS fees,
                   count(*) AS fills, count(*) FILTER (WHERE f.closed_pnl_micro <> 0) AS closing
              FROM fills f JOIN subscriptions s ON s.id = f.subscription_id JOIN strategies st ON st.id = s.strategy_id
             WHERE f.time >= CAST(:a AS timestamptz) AND f.time < CAST(:b AS timestamptz)
             GROUP BY s.user_id, st.name ORDER BY s.user_id, st.name""", a=start, b=end)
        by_user: dict[str, list[dict]] = {}
        for r in per:
            by_user.setdefault(str(r["user_id"]), []).append(r)
        created = 0
        for uid, items in by_user.items():
            from app.money import fmt_usd
            pnl = sum(int(i["pnl"]) for i in items)
            payload = {
                "date": d.isoformat(), "realized_pnl_micro": pnl, "fees_micro": sum(int(i["fees"]) for i in items),
                "fills": sum(int(i["fills"]) for i in items), "closed_trades": sum(int(i["closing"]) for i in items),
                "lines": [f"{i['strategy']}: {'+' if int(i['pnl']) > 0 else ''}{fmt_usd(int(i['pnl']))}" for i in items][:20],
            }
            created += len(_db.rows(conn, """
                INSERT INTO alerts (user_id, severity, kind, payload, dedup_key, created_at)
                VALUES (CAST(:u AS uuid), 'info', 'daily_pnl_summary', CAST(:p AS jsonb), :k, CAST(:now AS timestamptz))
                ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING RETURNING id""",
                u=uid, p=_db.jdump(payload), k=f"daily_pnl:{uid}:{d.isoformat()}", now=now))
    return {"date": d.isoformat(), "users": len(by_user), "created": created}
