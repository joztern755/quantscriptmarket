"""User alert kinds, routing policy and per-kind mute preferences (SPEC §12 "User alerts on Telegram + email",
"Email volume policy").

Routing (owner decision, 30 Sep 2026):
  * in-app: every alert (the `alerts` row itself; muting never hides it from the in-app list).
  * Telegram: every alert, unless the user muted a non-mandatory kind.
  * email: ONLY mandatory (*) kinds and security / money events. Trade alerts and the daily PnL summary are
    Telegram + in-app only. Unknown kinds are emailed only when critical.
Mandatory kinds can never be muted (API refuses; the DB CHECK on alert_prefs refuses too — keep
MANDATORY_KINDS in sync with migrations/0007_alerts.sql).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from app.errors import ValidationFailed

from . import _db

__all__ = [
    "KindSpec", "CATALOG", "MANDATORY_KINDS", "GROUPS", "spec_for", "route", "is_mandatory",
    "muted_kinds", "set_mutes", "prefs_view",
]


@dataclass(frozen=True)
class KindSpec:
    kind: str
    label: str
    group: str
    mandatory: bool = False
    email: bool = False            # mandatory / security / money → also by email
    telegram: bool = True
    listed: bool = True            # shown on the #/alerts settings page


GROUPS: tuple[tuple[str, str], ...] = (
    ("agent", "Trading agent & approvals"),
    ("trades", "Trades & PnL"),
    ("balance", "Fee balance & billing"),
    ("money", "Deposits & withdrawals"),
    ("security", "Account security"),
    ("markets", "Markets & strategies"),
)

_SPECS: tuple[KindSpec, ...] = (
    # agent / approvals
    KindSpec("agent_expiring", "Agent approval expiring (14, 7, 3, 1 days before)", "agent", mandatory=True, email=True),
    KindSpec("agent_expired", "Agent approval expired (re-approve link)", "agent", mandatory=True, email=True),
    KindSpec("agent_revoked", "Agent approval revoked or replaced on-chain", "agent", mandatory=True, email=True),
    KindSpec("builder_approval_missing", "Builder-fee approval missing", "agent", mandatory=True, email=True),
    # trades (Telegram + in-app only)
    KindSpec("trade_opened", "Trade opened", "trades"),
    KindSpec("trade_closed", "Trade closed", "trades"),
    KindSpec("trade_resized", "Position resized", "trades"),
    KindSpec("trade_pnl", "Realized profit / loss per closed trade", "trades"),
    KindSpec("daily_pnl_summary", "Daily PnL summary (00:15 UTC)", "trades"),
    KindSpec("user_drawdown", "Large drawdown (over 20% of allocation in 24h)", "trades"),
    # balance / billing
    KindSpec("balance_low", "Fee balance low (50% / 20% of monthly need)", "balance", mandatory=True, email=True),
    KindSpec("balance_empty", "Fee balance empty", "balance", mandatory=True, email=True),
    KindSpec("subscription_past_due", "Subscription past due", "balance", mandatory=True, email=True),
    KindSpec("subscription_reduce_only", "Subscription switched to reduce-only", "balance", mandatory=True, email=True),
    KindSpec("profit_share_charged", "Profit share charged", "balance", email=True),
    KindSpec("subscription_renewed", "Subscription renewed", "balance", email=True),
    KindSpec("plan_past_due", "Plan renewal failed", "balance", email=True),
    KindSpec("plan_downgraded", "Plan downgraded", "balance", email=True),
    # deposits / withdrawals
    KindSpec("topup_credited", "Deposit credited", "money", email=True),
    KindSpec("topup_failed", "Top-up failed", "money", email=True),
    KindSpec("topup_held", "Deposit held for review", "money", email=True),
    KindSpec("stripe_refund", "Deposit refunded", "money", mandatory=True, email=True),
    KindSpec("stripe_dispute", "Deposit disputed", "money", mandatory=True, email=True),
    KindSpec("deposit_refunded", "Deposit refunded", "money", mandatory=True, email=True, listed=False),
    KindSpec("deposit_disputed", "Deposit disputed", "money", mandatory=True, email=True, listed=False),
    KindSpec("withdrawal_requested", "Withdrawal requested", "money", mandatory=True, email=True),
    KindSpec("withdrawal_sent", "Withdrawal sent", "money", mandatory=True, email=True),
    KindSpec("withdrawal_rejected", "Withdrawal rejected", "money", mandatory=True, email=True),
    # security
    KindSpec("new_device_login", "Sign-in from a new device", "security", mandatory=True, email=True),
    KindSpec("login_new_country", "Sign-in from a new country", "security", mandatory=True, email=True),
    KindSpec("new_country_login", "Sign-in from a new country", "security", mandatory=True, email=True, listed=False),
    KindSpec("mfa_changed", "Two-factor authentication changed", "security", mandatory=True, email=True),
    KindSpec("mfa_reset", "Two-factor authentication reset", "security", mandatory=True, email=True, listed=False),
    KindSpec("alert_email_changed", "Alert email address changed", "security", mandatory=True, email=True),
    KindSpec("telegram_unreachable", "Telegram alerts stopped reaching you", "security", mandatory=True, email=True,
             telegram=False),
    # markets / strategies
    KindSpec("market_paused", "Kill switch / pause on a market you trade", "markets", mandatory=True, email=True),
    KindSpec("strategy_paused", "Strategy paused", "markets", mandatory=True, email=True),
    KindSpec("signal_stale", "Strategy signal stale", "markets"),
    # system
    # sent inline by POST /v1/alerts/test (both channels); the worker never re-sends it
    KindSpec("test_alert", "Test alert", "security", telegram=False, listed=False),
)

CATALOG: dict[str, KindSpec] = {s.kind: s for s in _SPECS}
MANDATORY_KINDS: frozenset[str] = frozenset(s.kind for s in _SPECS if s.mandatory)


def spec_for(kind: str) -> KindSpec | None:
    return CATALOG.get(kind)


def is_mandatory(kind: str) -> bool:
    return kind in MANDATORY_KINDS


def route(kind: str, severity: str) -> tuple[bool, bool, bool]:
    """(telegram, email, mandatory) for a user alert of this kind/severity, before mutes and contacts."""
    s = CATALOG.get(kind)
    if s is None:
        return True, severity == "critical", False
    return s.telegram, s.email, s.mandatory


# ------------------------------------------------------------------------------------------ preferences (SQL)
def muted_kinds(conn: Any, user_ids: Iterable[str]) -> dict[str, set[str]]:
    """{user_id: {muted kinds}} — mandatory kinds are never returned (defence in depth)."""
    ids = sorted({str(u) for u in user_ids})
    if not ids:
        return {}
    out: dict[str, set[str]] = {}
    for r in _db.rows(conn, """SELECT user_id, kind FROM alert_prefs
                               WHERE muted AND user_id = ANY(CAST(:ids AS uuid[]))""", ids=ids):
        if r["kind"] not in MANDATORY_KINDS:
            out.setdefault(str(r["user_id"]), set()).add(str(r["kind"]))
    return out


def set_mutes(conn: Any, user_id: str, changes: Mapping[str, bool], now: datetime) -> None:
    """Upsert mute switches. Unknown or unlisted kinds → 422; muting a mandatory kind → 422."""
    if not changes:
        raise ValidationFailed("no changes")
    if len(changes) > len(CATALOG):
        raise ValidationFailed("too many changes")
    for kind, muted in changes.items():
        s = CATALOG.get(kind)
        if s is None or not s.listed:
            raise ValidationFailed("unknown alert kind", kind=str(kind)[:64])
        if not isinstance(muted, bool):
            raise ValidationFailed("muted must be true or false", kind=kind)
        if muted and s.mandatory:
            raise ValidationFailed("this alert is mandatory and cannot be muted", kind=kind)
    for kind, muted in sorted(changes.items()):
        _db.rows(conn, """
            INSERT INTO alert_prefs (user_id, kind, muted, updated_at)
            VALUES (CAST(:u AS uuid), :k, :m, CAST(:now AS timestamptz))
            ON CONFLICT (user_id, kind) DO UPDATE SET muted = EXCLUDED.muted, updated_at = EXCLUDED.updated_at
            RETURNING kind""", u=user_id, k=kind, m=bool(muted), now=now)


def prefs_view(muted: set[str]) -> list[dict[str, Any]]:
    """Settings-page rows (listed kinds only), grouped in GROUPS order."""
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    labels = dict(GROUPS)
    out = []
    for s in sorted((s for s in _SPECS if s.listed), key=lambda s: order.get(s.group, 99)):
        out.append({
            "kind": s.kind, "label": s.label, "group": s.group, "group_label": labels.get(s.group, s.group),
            "mandatory": s.mandatory, "muted": (s.kind in muted) and not s.mandatory,
            "channels": [c for c, on in (("in_app", True), ("telegram", s.telegram), ("email", s.email)) if on],
        })
    return out
