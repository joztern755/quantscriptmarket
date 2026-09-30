"""Plain-text templates for USER alerts (Telegram + email). SPEC §12 alert kinds.

Payload contract per kind (producers: events_outbox from the data jobs (0006, app/jobs_data), settlement,
payments, API). Keys match what app/jobs_data emits; alternatives in brackets are accepted too.
  trade_opened / trade_closed / trade_resized   (app/jobs_data/fills.trade_events)
        coin, side ("buy"|"sell"), size, avg_px (decimal str), fees_micro [fee_micro], builder_fee_micro,
        notional_micro, realized_pnl_micro, net_pnl_micro, position_after [position_size], liquidation (bool),
        strategy (name; added by the delivery worker from strategy_id)
  trade_pnl            coin, net_pnl_micro [realized_pnl_micro], fees_micro, strategy — derived by the worker from
                       trade_closed / trade_resized events with a non-zero realized PnL (mutable separately)
  daily_pnl_summary    date (YYYY-MM-DD), realized_pnl_micro, fees_micro, fills, closed_trades, lines (list[str])
  agent_expiring       days_left, valid_until_ms [expires_at], master [wallet] (short address) — app/jobs_data/agents
  agent_expired / agent_revoked   master [wallet / master_address], valid_until_ms, reason
  builder_approval_missing        optional master [wallet]
  balance_low / balance_empty        balance_micro, threshold_bps, need_micro
  subscription_past_due / subscription_reduce_only   strategy (name), optional subscription
  profit_share_charged amount_micro, optional strategy, profit_micro, rate_bps
  topup_credited / topup_failed / topup_held / stripe_refund / stripe_dispute   amount_micro, optional method
  withdrawal_requested / withdrawal_sent / withdrawal_rejected                  amount_micro, optional request_id
  new_device_login     optional device, country ; login_new_country / new_country_login: country
  mfa_changed / mfa_reset   optional change
  market_paused        scope (coin or "all markets"), optional cause ; strategy_paused: strategy
  signal_stale         optional strategy ; telegram_unreachable: reason, pause_at ; alert_email_changed: email
Missing fields render as "—"; unknown kinds fall back to app.alerts.notifier.render (its TEMPLATES, then a
generic "key: value" body). Every value is sanitised (secrets redacted, 0x addresses shortened, emails masked),
so messages never contain full addresses, keys or tokens.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from app.money import fmt_usd

from .notifier import Alert, Severity, render as notifier_render, sanitize_text

__all__ = ["render_user_alert", "USER_TEMPLATES", "telegram_text"]

DASH = "—"


class P:
    """Payload accessor: sanitised strings with '—' defaults."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.d = {str(k): v for k, v in dict(data or {}).items() if not str(k).startswith("_")}

    def s(self, key: str, default: str = DASH) -> str:
        v = self.d.get(key)
        if v is None or v == "":
            return default
        if isinstance(v, (list, tuple)):
            return sanitize_text(", ".join(str(x) for x in v))
        return sanitize_text(str(v))

    def usd(self, key: str, *, sign: bool = False) -> str:
        v = self.d.get(key)
        if isinstance(v, bool) or not isinstance(v, int):
            try:
                v = int(str(v))
            except (TypeError, ValueError):
                return DASH
        txt = fmt_usd(v)
        return ("+" + txt) if sign and v > 0 else txt

    def int(self, key: str) -> int | None:
        v = self.d.get(key)
        if isinstance(v, bool):
            return None
        try:
            return int(str(v))
        except (TypeError, ValueError):
            return None

    def pct_bps(self, key: str) -> str:
        v = self.d.get(key)
        try:
            d = Decimal(str(v))
        except (InvalidOperation, ValueError):
            return DASH
        if not d.is_finite():
            return DASH
        return f"{(d / 100).normalize():f}%"

    def side(self) -> str:
        v = str(self.d.get("side") or "").lower()
        return {"buy": "Buy", "b": "Buy", "long": "Buy", "sell": "Sell", "a": "Sell", "short": "Sell"}.get(v, DASH)

    def lines(self, key: str) -> list[str]:
        v = self.d.get(key)
        if not isinstance(v, (list, tuple)):
            return []
        return [sanitize_text(str(x)) for x in v[:20]]


Tmpl = Callable[[P, str], tuple[str, str]]


def _dash(origin: str) -> str:
    return f"{origin.rstrip('/')}/#/dashboard"


def _fee_key(p: P) -> str:
    return "fees_micro" if p.d.get("fees_micro") is not None else "fee_micro"


def _pnl_key(p: P) -> str:
    return "net_pnl_micro" if p.d.get("net_pnl_micro") is not None else "realized_pnl_micro"


def _trade(verb: str) -> Tmpl:
    def t(p: P, origin: str) -> tuple[str, str]:
        lines = [f"{p.s('strategy', 'Your strategy')}: {p.side()} {p.s('size')} {p.s('coin')} @ avg {p.s('avg_px')}"]
        if p.d.get("notional_micro") is not None:
            lines.append(f"Notional: {p.usd('notional_micro')}")
        fees = f"Fees: {p.usd(_fee_key(p))}"
        if p.d.get("builder_fee_micro") is not None:
            fees += f" (incl. builder fee {p.usd('builder_fee_micro')})"
        lines.append(fees)
        pos = p.d.get("position_after", p.d.get("position_size"))
        if pos is not None:
            lines.append(f"Position now: {sanitize_text(str(pos))} {p.s('coin')}")
        pnl = p.int(_pnl_key(p))
        if verb != "opened" and pnl:
            lines.append(f"Realized PnL: {p.usd(_pnl_key(p), sign=True)} (after fees)")
        if p.d.get("liquidation") is True:
            lines.append("This fill was a LIQUIDATION.")
        return f"Trade {verb}: {p.s('coin')}", "\n".join(lines)
    return t


def _trade_pnl(p: P, origin: str) -> tuple[str, str]:
    k = _pnl_key(p)
    v = p.int(k)
    word = "profit" if (v or 0) >= 0 else "loss"
    fee = f" (after {p.usd(_fee_key(p))} fees)" if p.d.get(_fee_key(p)) is not None else " (after fees)"
    return (f"Realized {word}: {p.usd(k, sign=True)} on {p.s('coin')}",
            f"{p.s('strategy', 'Your strategy')} closed a trade on {p.s('coin')}: {p.usd(k, sign=True)}{fee}.")


def _daily(p: P, origin: str) -> tuple[str, str]:
    body = [f"Realized PnL on {p.s('date')} (UTC): {p.usd('realized_pnl_micro', sign=True)} after "
            f"{p.usd('fees_micro')} fees; {p.s('fills', '0')} fills, {p.s('closed_trades', '0')} closing."]
    body += p.lines("lines")
    body.append("Funding payments and open-position PnL are not included. Details: " + _dash(origin))
    return f"Daily PnL {p.s('date')}: {p.usd('realized_pnl_micro', sign=True)}", "\n".join(body)


def _wallet(p: P) -> str:
    for k in ("master", "wallet", "master_address"):
        if p.d.get(k):
            return f" for wallet {p.s(k)}"
    return ""


def _expiry(p: P) -> str:
    ms = p.int("valid_until_ms")
    if ms is not None and ms > 0:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return p.s("expires_at")


def _agent_expiring(p: P, origin: str) -> tuple[str, str]:
    days = p.int("days_left")
    when = ("today" if days == 0 else f"in {days} day{'s' if days != 1 else ''}") if days is not None else "soon"
    return (f"Agent approval expires {when}",
            f"Your trading agent approval{_wallet(p)} expires {when} ({_expiry(p)}). After that no orders can be "
            f"placed or closed for you. Re-approve in one step: {_dash(origin)}")


def _agent_expired(p: P, origin: str) -> tuple[str, str]:
    return ("Agent approval expired — trading stopped",
            f"Your trading agent approval{_wallet(p)} has expired, so strategies can no longer open or close positions "
            f"for you. Open positions stay as they are. Re-approve now: {_dash(origin)}")


def _builder_missing(p: P, origin: str) -> tuple[str, str]:
    return ("Builder-fee approval missing",
            f"The builder-fee approval{_wallet(p)} on Hyperliquid is missing or too low, so no new orders can be "
            f"placed. Approve it again: {_dash(origin)}")


def _balance(title: str, tail: str) -> Tmpl:
    def t(p: P, origin: str) -> tuple[str, str]:
        return (title,
                f"Your fee balance is {p.usd('balance_micro')} (threshold {p.pct_bps('threshold_bps')} of the estimated "
                f"monthly need of {p.usd('need_micro')}). {tail} Top up: {_dash(origin)}")
    return t


def _sub_status(title: str, tail: str) -> Tmpl:
    def t(p: P, origin: str) -> tuple[str, str]:
        return title, f"{p.s('strategy')}: {tail} Top up your fee balance: {_dash(origin)}"
    return t


def _amount(title: str, text: str) -> Tmpl:
    def t(p: P, origin: str) -> tuple[str, str]:
        method = f" ({p.s('method')})" if p.d.get("method") else ""
        ref = f" Reference: {p.s('request_id')}." if p.d.get("request_id") else ""
        return title, text.format(amount=p.usd("amount_micro"), method=method) + ref
    return t


def _profit_share(p: P, origin: str) -> tuple[str, str]:
    parts = [f"{p.usd('amount_micro')} profit share was charged from your fee balance"]
    if p.d.get("strategy"):
        parts.append(f" for {p.s('strategy')}")
    if p.d.get("profit_micro") is not None:
        parts.append(f" on {p.usd('profit_micro')} of new profit above the high-water mark")
    if p.d.get("rate_bps") is not None:
        parts.append(f" at {p.pct_bps('rate_bps')}")
    return "Profit share charged", "".join(parts) + "."


def _simple(title: str, body: str) -> Tmpl:
    return lambda p, origin: (title, body.format(origin=origin.rstrip("/"), dash=_dash(origin)))


def _login_country(p: P, origin: str) -> tuple[str, str]:
    return ("New sign-in location",
            f"Your account was signed into from a new country ({p.s('country')}). If this was not you, secure your "
            "Google/Apple account now and contact support.")


def _new_device(p: P, origin: str) -> tuple[str, str]:
    where = f" ({p.s('country')})" if p.d.get("country") else ""
    dev = f" on {p.s('device')}" if p.d.get("device") else ""
    return ("New device sign-in",
            f"Your account was signed into from a new device{dev}{where}. If this was not you, secure your "
            "Google/Apple account now and contact support.")


def _market_paused(p: P, origin: str) -> tuple[str, str]:
    if str(p.d.get("cause") or "") == "kill switch":
        return (f"Trading halted on {p.s('scope')}",
                f"A kill switch halted all trading on {p.s('scope')}, a market your strategies trade. No orders are "
                "placed there (open positions stay as they are) until it is lifted.")
    cause = f" Cause: {p.s('cause')}." if p.d.get("cause") else ""
    return (f"New entries paused on {p.s('scope')}",
            f"New entries are paused on {p.s('scope')}, a market your strategies trade. Exits still run.{cause}")


def _unreachable(p: P, origin: str) -> tuple[str, str]:
    why = {"stopped": "you sent /stop to the bot", "blocked": "the bot was blocked",
           "chat_not_found": "your Telegram chat can no longer be reached"}.get(str(p.d.get("reason")), "delivery failed")
    return ("Telegram alerts stopped",
            f"We can no longer send you Telegram alerts ({why}). Telegram alerts are required while you have "
            f"subscriptions: new entries pause at {p.s('pause_at')} unless you link Telegram again on the Alerts "
            f"page ({origin.rstrip('/')}/#/alerts). Exits keep running.")


USER_TEMPLATES: dict[str, Tmpl] = {
    "trade_opened": _trade("opened"),
    "trade_closed": _trade("closed"),
    "trade_resized": _trade("resized"),
    "trade_pnl": _trade_pnl,
    "daily_pnl_summary": _daily,
    "agent_expiring": _agent_expiring,
    "agent_expired": _agent_expired,
    "builder_approval_missing": _builder_missing,
    "agent_revoked": lambda p, o: ("Agent approval revoked",
                                   f"The trading agent approval{_wallet(p)} is no longer active on-chain, so strategies "
                                   "cannot trade this wallet (open positions stay as they are). If you did not do this, "
                                   f"check your wallet. Reconnect: {_dash(o)}"),
    "user_drawdown": lambda p, o: ("Large drawdown",
                                   f"A subscription lost {p.usd('pnl_24h_micro')} over 24 hours on an allocation of "
                                   f"{p.usd('allocation_micro')}. Review it: {_dash(o)}"),
    "subscription_renewed": lambda p, o: ("Subscription renewed",
                                          f"{p.s('strategy')} was renewed for {p.usd('amount_micro')} from your fee "
                                          "balance."),
    "plan_past_due": lambda p, o: ("Plan renewal failed",
                                   f"Your {p.s('plan')} plan renewal of {p.usd('due_micro')} could not be paid "
                                   f"(available {p.usd('available_micro')}). Top up: {_dash(o)}"),
    "plan_downgraded": lambda p, o: ("Plan downgraded",
                                     f"Your plan changed from {p.s('from')} to {p.s('to')} because the renewal could not "
                                     "be paid."),
    "balance_low": _balance("Fee balance running low",
                            "Below 0% of the need, subscriptions go past due and then reduce-only (exits only)."),
    "balance_empty": _balance("Fee balance empty",
                              "Subscriptions will go past due and switch to reduce-only (exits only) after the grace "
                              "period."),
    "subscription_past_due": _sub_status("Subscription past due",
                                         "the renewal could not be paid from your fee balance. Top up within the "
                                         "72-hour grace period to avoid reduce-only mode."),
    "subscription_reduce_only": _sub_status("Subscription set to reduce-only",
                                            "the subscription can no longer open new positions (exits still run)."),
    "profit_share_charged": _profit_share,
    "topup_credited": _amount("Deposit credited", "{amount} was added to your fee balance{method}."),
    "topup_failed": _amount("Top-up failed", "Your top-up{method} did not complete. No money was added."),
    "topup_held": _amount("Deposit held for review", "A deposit of {amount}{method} is held for manual review."),
    "stripe_refund": _amount("Deposit refunded",
                             "A refund of {amount} was issued for a top-up; your fee balance was reduced by the same "
                             "amount."),
    "deposit_refunded": _amount("Deposit refunded",
                                "A refund of {amount} was issued for a top-up; your fee balance was reduced by the "
                                "same amount."),
    "stripe_dispute": _amount("Deposit disputed",
                              "A top-up payment was disputed with your bank; {amount} was removed from your fee "
                              "balance until the dispute is resolved."),
    "deposit_disputed": _amount("Deposit disputed",
                                "A top-up payment was disputed with your bank; {amount} was removed from your fee "
                                "balance until the dispute is resolved."),
    "withdrawal_requested": _amount("Withdrawal requested",
                                    "A withdrawal of {amount} from your fee balance was requested and awaits two "
                                    "approvals. If this was not you, contact support immediately."),
    "withdrawal_sent": _amount("Withdrawal sent", "Your withdrawal of {amount} was sent."),
    "withdrawal_rejected": _amount("Withdrawal rejected",
                                   "Your withdrawal of {amount} was rejected; the amount is back in your fee balance."),
    "new_device_login": _new_device,
    "login_new_country": _login_country,
    "new_country_login": _login_country,
    "mfa_changed": _simple("Two-factor authentication changed",
                           "Two-factor authentication on your account was changed. If this was not you, contact "
                           "support immediately."),
    "mfa_reset": _simple("Two-factor authentication reset",
                         "Two-factor authentication on your account was reset. If this was not you, contact support "
                         "immediately."),
    "alert_email_changed": lambda p, o: ("Alert email changed",
                                         f"Alerts for your account now go to {p.s('email')}. If this was not you, "
                                         "contact support immediately."),
    "market_paused": _market_paused,
    "strategy_paused": lambda p, o: ("Strategy paused",
                                     f"{p.s('strategy')} is paused: it will not open new positions until it resumes. "
                                     "Exits still run."),
    "signal_stale": lambda p, o: ("Strategy signal stale" + (f" ({p.s('coin')})" if p.d.get("coin") else ""),
                                  f"The latest signal for {p.s('strategy', 'your strategy')} is late or was rejected, "
                                  "so no new entries are placed until a fresh, verified signal arrives. Exits still "
                                  "run."),
    "telegram_unreachable": _unreachable,
    "test_alert": _simple("Test alert",
                          "This is a test alert from aijalon.trade. If you can read it, this channel works."),
}


def render_user_alert(kind: str, severity: str, payload: Mapping[str, Any], *, web_origin: str) -> tuple[str, str]:
    """(title, body) — plain text, sanitised."""
    p = P(payload)
    # rows written by notifier.InAppSink (executor / settlement) carry already-rendered, sanitised text only
    if isinstance(p.d.get("title"), str) and isinstance(p.d.get("body"), str) and set(p.d) <= {"title", "body", "coin", "key"}:
        return sanitize_text(p.d["title"]), sanitize_text(p.d["body"])[:3000]
    fn = USER_TEMPLATES.get(kind)
    if fn is not None:
        try:
            title, body = fn(p, web_origin or "https://aijalon.trade")
            return sanitize_text(title), body[:3000]
        except Exception:  # a template bug must never block delivery: fall through to the generic path
            pass
    if isinstance(p.d.get("title"), str) and isinstance(p.d.get("body"), str):
        return sanitize_text(p.d["title"]), sanitize_text(p.d["body"])[:3000]
    try:
        sev = Severity(severity)
    except ValueError:
        sev = Severity.INFO
    r = notifier_render(Alert(kind=str(kind), severity=sev, data=p.d))
    return r.title, r.body


def telegram_text(title: str, body: str, severity: str) -> str:
    head = {"critical": "URGENT: ", "warn": ""}.get(severity, "")
    return f"{head}{title}\n\n{body}\n\n— aijalon.trade"
