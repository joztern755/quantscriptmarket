"""User alert settings (SPEC §12 "User alerts on Telegram + email"): contacts, Telegram linking, email confirmation,
per-kind mutes, test alert — plus the executor-only jobs that deliver them.

  GET   /v1/alerts/settings                 contacts status + per-kind preferences (mandatory ones locked)
  GET   /v1/alerts/contacts                 contacts status only (the web polls it while the user links Telegram)
  POST  /v1/alerts/telegram/link            one-time https://t.me/<bot>?start=<token> (10 min, single use)
  POST  /v1/alerts/email/confirm-account    confirm the ACCOUNT email (provider-verified) as the alert email
  POST  /v1/alerts/email/start   (step-up)  send a 6-digit code to a new address
  POST  /v1/alerts/email/verify             check the code (5 attempts, 10 min) → alert email changed
  PATCH /v1/alerts/prefs                    {"muted": {"trade_opened": true, …}} (mandatory kinds refused)
  POST  /v1/alerts/test                     send a test alert to Telegram and email now

  executor service only (Cloud Scheduler OIDC):
  POST  /v1/internal/deliver-alerts         every minute → app.alerts.delivery.deliver_outbox
  POST  /v1/internal/daily-pnl-summary      00:15 UTC    → app.alerts.delivery.emit_daily_pnl_summaries
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends
from pydantic import Field, StrictBool, StringConstraints
from starlette.concurrency import run_in_threadpool

from app.alerts import delivery, prefs, telegram_bot, user_sinks
from app.api import schemas as S
from app.api.deps import (
    STEP_UP_MAX_AGE_SECONDS,
    AuthCtx,
    Services,
    check_step_up_claims,
    consented_user,
    get_services,
    scheduler_auth,
    step_up_user,
    user_limit,
)
from app.errors import AppError, Conflict

router = APIRouter(prefix="/alerts", tags=["alerts"])
internal_router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(scheduler_auth)])


class InvalidCode(AppError):
    http_status, code = 422, "invalid_code"


# ------------------------------------------------------------------------------------------------ schemas
class ContactsOut(S.Out):
    telegram: dict[str, Any]
    email: dict[str, Any]
    ready: bool
    missing: list[str]
    entries_allowed: bool
    entries_pause_at: Optional[str] = None


class PrefOut(S.Out):
    kind: str
    label: str
    group: str
    group_label: str
    mandatory: bool
    muted: bool
    channels: list[str]


class SettingsOut(S.Out):
    contacts: ContactsOut
    prefs: list[PrefOut]
    telegram_bot: Optional[str] = None
    email_policy: str = ("Telegram carries every alert. Email carries only mandatory alerts and security / money "
                         "events; trade alerts and the daily PnL summary are Telegram + in-app only.")


class LinkOut(S.Out):
    url: str
    expires_at: datetime


class EmailStartIn(S.In):
    email: Annotated[str, StringConstraints(min_length=3, max_length=254)]


class EmailStartOut(S.Out):
    sent_to: str
    expires_at: datetime


class EmailVerifyIn(S.In):
    code: Annotated[str, StringConstraints(pattern=r"^\s*\d{6}\s*$")]


class PrefsIn(S.In):
    muted: dict[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,63}$")], StrictBool] = Field(
        ..., min_length=1, max_length=64)


class TestOut(S.Out):
    results: dict[str, str]


# ------------------------------------------------------------------------------------------------ helpers
def _contacts(conn: Any, svc: Services, user_id: str) -> ContactsOut:
    return ContactsOut(**user_sinks.contact_status(conn, user_id, svc.now()).public())


def _settings(conn: Any, svc: Services, user_id: str) -> SettingsOut:
    muted = prefs.muted_kinds(conn, [user_id]).get(user_id, set())
    return SettingsOut(contacts=_contacts(conn, svc, user_id),
                       prefs=[PrefOut(**p) for p in prefs.prefs_view(muted)],
                       telegram_bot=(svc.settings.telegram_bot_username or None))


# ------------------------------------------------------------------------------------------------ routes
@router.get("/settings", response_model=SettingsOut)
def get_settings_(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> SettingsOut:
    with svc.db.begin() as conn:
        return _settings(conn, svc, ctx.user_id)


@router.get("/contacts", response_model=ContactsOut, dependencies=[user_limit("alerts_poll", 120, 60)])
def get_contacts(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> ContactsOut:
    with svc.db.begin() as conn:
        return _contacts(conn, svc, ctx.user_id)


@router.post("/telegram/link", response_model=LinkOut, dependencies=[user_limit("tg_link", 10, 3600)])
def telegram_link(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> LinkOut:
    """First link: consented session. RE-linking while a chat is linked re-routes mandatory alerts, so it needs a
    fresh sign-in (step-up) and warns the CURRENT chat + email (REVIEW_AUTH_API F2)."""
    now = svc.now()
    with svc.db.begin() as conn:
        linked = user_sinks.contact_status(conn, ctx.user_id, now).telegram == "linked"
    if linked:
        svc.auth.require_step_up(ctx.claims, STEP_UP_MAX_AGE_SECONDS)
        check_step_up_claims(ctx.claims, now)
    with svc.db.begin() as conn:
        link = telegram_bot.create_link(conn, user_id=ctx.user_id, now=now,
                                        bot_username=svc.settings.telegram_bot_username)
        if linked:
            # delivered by the alert worker to the chat linked NOW (and by email: critical) before any re-link
            svc.notifier.notify(conn, user_id=ctx.user_id, severity="critical", kind="alert_contacts_changed",
                                payload={"change": "telegram_relink_requested",
                                         "text": "A new Telegram chat is being linked to your aijalon.trade alerts. "
                                                 "If this was not you, sign in and contact support immediately."},
                                dedup_key=f"alert_contacts_changed:{ctx.user_id}:{link['expires_at'].isoformat()}")
        svc.audit.write(conn, actor=ctx.actor, action="alerts.telegram_link_created", target=f"user:{ctx.user_id}",
                        payload={"expires_at": link["expires_at"].isoformat(), "relink": linked}, ip_hash=ctx.ip_hash)
    return LinkOut(url=link["url"], expires_at=link["expires_at"])


@router.post("/email/confirm-account", response_model=ContactsOut, dependencies=[user_limit("email_confirm", 10, 3600)])
def email_confirm_account(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> ContactsOut:
    now = svc.now()
    with svc.db.begin() as conn:
        st = user_sinks.contact_status(conn, ctx.user_id, now)
        acct = (st.account_email or "").lower()
        if st.email_verified and st.email and st.email.lower() != acct:
            # switching back from another verified address is an email CHANGE → step-up (SPEC §12)
            svc.auth.require_step_up(ctx.claims, STEP_UP_MAX_AGE_SECONDS)
            check_step_up_claims(ctx.claims, now)
        email = user_sinks.confirm_account_email(conn, user_id=ctx.user_id, claims_email=ctx.claims.get("email"),
                                                 claims_email_verified=ctx.claims.get("email_verified"), now=now)
        if st.email_verified and st.email and st.email.lower() != email.lower():
            svc.notifier.notify(conn, user_id=ctx.user_id, severity="warn", kind="alert_email_changed",
                                payload={"email": email})
        svc.audit.write(conn, actor=ctx.actor, action="alerts.email_confirmed", target=f"user:{ctx.user_id}",
                        payload={"email": user_sinks.masked(email), "source": "account"}, ip_hash=ctx.ip_hash)
        return _contacts(conn, svc, ctx.user_id)


@router.post("/email/start", response_model=EmailStartOut, dependencies=[user_limit("email_code", 5, 3600)])
def email_start(body: EmailStartIn, ctx: AuthCtx = Depends(step_up_user),
                svc: Services = Depends(get_services)) -> EmailStartOut:
    email = user_sinks.normalize_email(body.email)
    with svc.db.begin() as conn:
        expires = user_sinks.start_email_verification(conn, user_id=ctx.user_id, email=email, now=svc.now(),
                                                      pepper=svc.config.pepper,
                                                      provider=user_sinks.email_provider(svc.settings))
        svc.audit.write(conn, actor=ctx.actor, action="alerts.email_code_sent", target=f"user:{ctx.user_id}",
                        payload={"email": user_sinks.masked(email)}, ip_hash=ctx.ip_hash)
    return EmailStartOut(sent_to=email, expires_at=expires)


@router.post("/email/verify", response_model=ContactsOut, dependencies=[user_limit("email_verify", 20, 3600)])
def email_verify(body: EmailVerifyIn, ctx: AuthCtx = Depends(consented_user),
                 svc: Services = Depends(get_services)) -> ContactsOut:
    now = svc.now()
    with svc.db.begin() as conn:   # commits the attempt counter even when the code is wrong
        res = user_sinks.verify_email_code(conn, user_id=ctx.user_id, code=body.code, now=now,
                                           pepper=svc.config.pepper)
        if res.ok:
            if res.previous_email:
                svc.notifier.notify(conn, user_id=ctx.user_id, severity="warn", kind="alert_email_changed",
                                    payload={"email": res.email})
            svc.audit.write(conn, actor=ctx.actor, action="alerts.email_verified", target=f"user:{ctx.user_id}",
                            payload={"email": user_sinks.masked(res.email),
                                     "previous": user_sinks.masked(res.previous_email)}, ip_hash=ctx.ip_hash)
            out = _contacts(conn, svc, ctx.user_id)
    if not res.ok:
        raise InvalidCode({"no_code": "request a new code first", "expired": "the code expired; request a new one",
                           "too_many_attempts": "too many wrong codes; request a new one"}.get(res.reason or "",
                                                                                               "wrong code"),
                          reason=res.reason, attempts_left=res.attempts_left)
    if res.previous_email:
        _notify_old_address(svc, res.previous_email, res.email or "")
    return out


def _notify_old_address(svc: Services, old: str, new: str) -> None:
    """Best effort: tell the PREVIOUS alert address that alerts moved (account-takeover signal)."""
    provider = user_sinks.email_provider(svc.settings)
    if provider is None:
        return
    try:
        provider.send(old, "[aijalon] Your alert email was changed",
                      f"Alerts for your aijalon.trade account now go to {user_sinks.masked(new)}. If this was not you, "
                      "secure your Google/Apple account and contact support immediately.")
    except Exception:  # noqa: BLE001 - informational only
        pass


@router.patch("/prefs", response_model=SettingsOut, dependencies=[user_limit("alert_prefs", 30, 60)])
def patch_prefs(body: PrefsIn, ctx: AuthCtx = Depends(consented_user),
                svc: Services = Depends(get_services)) -> SettingsOut:
    with svc.db.begin() as conn:
        prefs.set_mutes(conn, ctx.user_id, dict(body.muted), svc.now())
        svc.audit.write(conn, actor=ctx.actor, action="alerts.prefs_changed", target=f"user:{ctx.user_id}",
                        payload={"muted": {k: bool(v) for k, v in sorted(body.muted.items())}}, ip_hash=ctx.ip_hash)
        return _settings(conn, svc, ctx.user_id)


@router.post("/test", response_model=TestOut, dependencies=[user_limit("alert_test", 5, 600)])
def test_alert(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> TestOut:
    with svc.db.begin() as conn:
        st = user_sinks.contact_status(conn, ctx.user_id, svc.now())
        if not st.telegram_ok and not st.email_verified:
            raise Conflict("link Telegram or confirm an email first", reason="contacts_required")
        results = delivery.send_test_alert(conn, user_id=ctx.user_id, now=svc.now(), settings=svc.settings)
    return TestOut(results=results)


# ------------------------------------------------------------------------------------------------ jobs (executor)
def _job(svc: Services, name: str, result: dict[str, Any]) -> S.JobOut:
    with svc.db.begin() as conn:
        svc.audit.write(conn, actor="system:scheduler", action=f"job.{name}", target="",
                        payload={"result": {str(k): str(v)[:200] for k, v in list(result.items())[:20]}},
                        ip_hash=None)
    return S.JobOut(job=name, ok=True, result=result)


@internal_router.post("/deliver-alerts", response_model=S.JobOut)
async def deliver_alerts(svc: Services = Depends(get_services)) -> S.JobOut:
    result = await run_in_threadpool(delivery.deliver_outbox, svc.db, svc.now(), settings=svc.settings)
    # every-minute job: audit only when something happened (keeps the audit chain meaningful)
    if any(int(v or 0) for v in result.values()):
        return await run_in_threadpool(_job, svc, "deliver-alerts", result)
    return S.JobOut(job="deliver-alerts", ok=True, result=result)


class DailyPnlIn(S.In):
    day: Optional[date] = None


@internal_router.post("/daily-pnl-summary", response_model=S.JobOut)
async def daily_pnl_summary(body: Optional[DailyPnlIn] = Body(None),
                            svc: Services = Depends(get_services)) -> S.JobOut:
    result = await run_in_threadpool(delivery.emit_daily_pnl_summaries, svc.db, svc.now(),
                                     day=body.day if body else None)
    return await run_in_threadpool(_job, svc, "daily-pnl-summary", result)
