"""Anomaly rules → Alert objects (SPEC §5.5). Pure: each rule takes observed values and returns an `Alert`
or None. Delivery, persistence and the auto kill-switch live in `app/alerts/notifier.py`.

Severity policy:
- critical + auto_pause_market: mark/oracle divergence above the risk cap, OI spike > 50% in 1h,
  funding spike above the optional critical threshold. The notifier pauses new entries on `market` and
  pages ops (Telegram + email).
- critical (no market pause): agent approval revoked/changed on-chain (that user's trading stops),
  ledger ↔ on-chain reconciliation mismatch > $1.
- warn: mark/oracle divergence above half the cap, funding spike, user drawdown > 20% of allocation in 24h,
  rejection burst, login from a new country, MFA reset, large withdrawal/payout request.
- info: ordinary withdrawal/payout request (maker-checker queue).

Dedupe `key`: "<kind>:<subject>:<bucket>". Condition-type alerts bucket by UTC hour (drawdown by UTC day) so
a persisting condition re-alerts at most once per bucket; event-type alerts use the event's own id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from collections.abc import Collection
from typing import Any

from app.config import RiskLimits
from app.money import BPS, usd

from ._common import require_aware, require_int, require_non_negative

__all__ = [
    "INFO", "WARN", "CRITICAL",
    "Alert",
    "AUTO_PAUSE_KINDS",
    "mark_oracle_divergence",
    "oi_spike",
    "funding_spike",
    "user_drawdown",
    "reject_burst",
    "agent_revoked",
    "new_country_login",
    "mfa_reset",
    "withdrawal_request",
    "reconciliation_mismatch",
    "market_alerts",
]

INFO, WARN, CRITICAL = "info", "warn", "critical"
_R = RiskLimits()
DEFAULT_FUNDING_THRESHOLD_PER_HOUR = Decimal("0.001")   # 0.1% per hour
#: kinds that may carry auto_pause_market=True (when critical)
AUTO_PAUSE_KINDS = frozenset({"mark_oracle_divergence", "oi_spike", "funding_spike"})


@dataclass(frozen=True)
class Alert:
    severity: str                  # info | warn | critical
    kind: str
    key: str                       # dedupe key
    payload: dict[str, Any] = field(default_factory=dict)   # JSON-safe (Decimals as str, datetimes ISO)
    auto_pause_market: bool = False
    market: str | None = None
    user_id: str | None = None

    @property
    def page_ops(self) -> bool:
        return self.severity == CRITICAL


def _hour(now: datetime) -> str:
    return require_aware("now", now).strftime("%Y-%m-%dT%H")


def _day(now: datetime) -> str:
    return require_aware("now", now).strftime("%Y-%m-%d")


def _dec(name: str, v: object) -> Decimal:
    if not isinstance(v, Decimal) or not v.is_finite():
        raise TypeError(f"{name} must be a finite Decimal")
    return v


# ---------------------------------------------------------------- market rules

def mark_oracle_divergence(
    coin: str,
    mark_px: Decimal,
    oracle_px: Decimal,
    now: datetime,
    *,
    warn_bps: int | None = None,
    critical_bps: int = _R.max_mark_oracle_dev_bps,
) -> Alert | None:
    """|mark − oracle| / oracle > critical_bps (default 2%) → critical + auto-pause; > warn_bps (default half
    of critical) → warn. A non-positive oracle price is itself critical (fail closed)."""
    mark, oracle = _dec("mark_px", mark_px), _dec("oracle_px", oracle_px)
    warn = critical_bps // 2 if warn_bps is None else warn_bps
    if oracle <= 0:
        dev = None
        sev = CRITICAL
    else:
        dev = abs(mark - oracle) * BPS / oracle
        if dev > critical_bps:
            sev = CRITICAL
        elif dev > warn:
            sev = WARN
        else:
            return None
    return Alert(
        sev, "mark_oracle_divergence", f"mark_oracle_divergence:{coin}:{_hour(now)}",
        {"coin": coin, "mark_px": str(mark), "oracle_px": str(oracle),
         "deviation_bps": None if dev is None else str(dev.quantize(Decimal("0.01"))),
         "critical_bps": critical_bps},
        auto_pause_market=sev == CRITICAL, market=coin,
    )


def oi_spike(
    coin: str,
    oi_now_micro: int,
    oi_1h_ago_micro: int,
    now: datetime,
    *,
    threshold_bps: int = _R.oi_spike_alert_bps,
) -> Alert | None:
    """Open interest up by more than threshold (default 50%) within 1h → critical + auto-pause (JELLY lesson).
    From zero to any positive OI counts as a spike (fail closed)."""
    cur = require_non_negative("oi_now_micro", oi_now_micro)
    prev = require_non_negative("oi_1h_ago_micro", oi_1h_ago_micro)
    if cur <= prev:
        return None
    if prev > 0 and (cur - prev) * BPS <= prev * threshold_bps:
        return None
    growth = None if prev == 0 else ((cur - prev) * BPS) // prev
    return Alert(
        CRITICAL, "oi_spike", f"oi_spike:{coin}:{_hour(now)}",
        {"coin": coin, "oi_now_micro": cur, "oi_1h_ago_micro": prev, "growth_bps": growth, "threshold_bps": threshold_bps},
        auto_pause_market=True, market=coin,
    )


def funding_spike(
    coin: str,
    funding_rate_per_hour: Decimal,
    now: datetime,
    *,
    threshold: Decimal = DEFAULT_FUNDING_THRESHOLD_PER_HOUR,
    critical_threshold: Decimal | None = None,
) -> Alert | None:
    """|hourly funding rate| > threshold (default 0.1%/h) → warn; ≥ critical_threshold (if configured) →
    critical + auto-pause. Rates are fractions (Decimal("0.001") = 0.1%)."""
    f = _dec("funding_rate_per_hour", funding_rate_per_hour)
    if abs(f) <= threshold:
        return None
    crit = critical_threshold is not None and abs(f) >= critical_threshold
    return Alert(
        CRITICAL if crit else WARN, "funding_spike", f"funding_spike:{coin}:{_hour(now)}",
        {"coin": coin, "funding_rate_per_hour": str(f), "threshold": str(threshold),
         "critical_threshold": None if critical_threshold is None else str(critical_threshold)},
        auto_pause_market=crit, market=coin,
    )


# ---------------------------------------------------------------- user / subscription rules

def user_drawdown(
    user_id: str,
    subscription_id: str,
    allocation_micro: int,
    pnl_24h_micro: int,
    now: datetime,
    *,
    threshold_bps: int = _R.user_drawdown_alert_bps,
) -> Alert | None:
    """Loss over the last 24h strictly greater than threshold (default 20%) of allocation → warn."""
    alloc = require_non_negative("allocation_micro", allocation_micro)
    pnl = require_int("pnl_24h_micro", pnl_24h_micro)
    if alloc == 0 or pnl >= 0 or (-pnl) * BPS <= alloc * threshold_bps:
        return None
    return Alert(
        WARN, "user_drawdown", f"user_drawdown:{subscription_id}:{_day(now)}",
        {"subscription_id": subscription_id, "allocation_micro": alloc, "pnl_24h_micro": pnl,
         "loss_bps": ((-pnl) * BPS) // alloc, "threshold_bps": threshold_bps},
        user_id=user_id,
    )


def reject_burst(
    subscription_id: str,
    user_id: str | None,
    rejects_in_window: int,
    now: datetime,
    *,
    threshold: int = _R.consecutive_reject_breaker,
) -> Alert | None:
    """Order rejections for a subscription reached `threshold` (default = breaker limit 3) → warn."""
    n = require_non_negative("rejects_in_window", rejects_in_window)
    if n < threshold:
        return None
    return Alert(
        WARN, "reject_burst", f"reject_burst:{subscription_id}:{_hour(now)}",
        {"subscription_id": subscription_id, "rejects": n, "threshold": threshold},
        user_id=user_id,
    )


def agent_revoked(
    user_id: str,
    master_address: str,
    expected_agent_address: str,
    onchain_agent_addresses: Collection[str],
    now: datetime,
) -> Alert | None:
    """Our agent is no longer among the account's approved agents on-chain (revoked, or replaced by a new
    approval under the same name) → critical for that user. Addresses compare case-insensitively."""
    require_aware("now", now)
    expected = expected_agent_address.strip().lower()
    onchain = {a.strip().lower() for a in onchain_agent_addresses if a}
    if expected in onchain:
        return None
    return Alert(
        CRITICAL, "agent_revoked", f"agent_revoked:{master_address.lower()}:{expected}",
        {"master_address": master_address.lower(), "agent_address": expected, "onchain_agents": sorted(onchain)},
        user_id=user_id,
    )


def new_country_login(user_id: str, country: str, known_countries: Collection[str], now: datetime) -> Alert | None:
    """Login from an ISO country not seen before for this user → warn. No alert for the very first login
    (no known countries yet) or when the country is unknown/blank."""
    require_aware("now", now)
    c = (country or "").strip().upper()
    known = {k.strip().upper() for k in known_countries if k}
    if not c or not known or c in known:
        return None
    return Alert(
        WARN, "new_country_login", f"new_country_login:{user_id}:{c}",
        {"country": c, "known_countries": sorted(known)}, user_id=user_id,
    )


def mfa_reset(user_id: str, event_id: str, now: datetime) -> Alert:
    """Any MFA factor reset/removal → warn (possible account takeover); always emitted."""
    return Alert(WARN, "mfa_reset", f"mfa_reset:{user_id}:{event_id}",
                 {"event_id": event_id, "at": require_aware("now", now).isoformat()}, user_id=user_id)


def withdrawal_request(
    request_kind: str,
    request_id: str,
    user_id: str | None,
    amount_micro: int,
    now: datetime,
    *,
    large_threshold_micro: int = usd(10_000),
) -> Alert:
    """Every withdrawal/payout request is surfaced (info) for the maker-checker queue; ≥ threshold → warn."""
    if request_kind not in ("withdrawal", "payout"):
        raise ValueError("request_kind must be 'withdrawal' or 'payout'")
    amt = require_non_negative("amount_micro", amount_micro)
    return Alert(
        WARN if amt >= large_threshold_micro else INFO, f"{request_kind}_request", f"{request_kind}_request:{request_id}",
        {"request_id": request_id, "amount_micro": amt, "at": require_aware("now", now).isoformat()},
        user_id=user_id,
    )


def reconciliation_mismatch(
    scope: str,
    expected_micro: int,
    actual_micro: int,
    now: datetime,
    *,
    threshold_micro: int = usd(1),
) -> Alert | None:
    """Ledger vs on-chain (builder fees, treasury USDC, positions…) differ by MORE than threshold ($1) →
    critical (page ops)."""
    exp, act = require_int("expected_micro", expected_micro), require_int("actual_micro", actual_micro)
    diff = act - exp
    if abs(diff) <= threshold_micro:
        return None
    return Alert(
        CRITICAL, "reconciliation_mismatch", f"reconciliation_mismatch:{scope}:{_hour(now)}",
        {"scope": scope, "expected_micro": exp, "actual_micro": act, "diff_micro": diff, "threshold_micro": threshold_micro},
    )


def market_alerts(
    coin: str,
    mark_px: Decimal,
    oracle_px: Decimal,
    oi_now_micro: int,
    oi_1h_ago_micro: int,
    funding_rate_per_hour: Decimal,
    now: datetime,
    *,
    funding_critical_threshold: Decimal | None = None,
) -> list[Alert]:
    """All market rules for one traded coin, with default thresholds."""
    out = [
        mark_oracle_divergence(coin, mark_px, oracle_px, now),
        oi_spike(coin, oi_now_micro, oi_1h_ago_micro, now),
        funding_spike(coin, funding_rate_per_hour, now, critical_threshold=funding_critical_threshold),
    ]
    return [a for a in out if a is not None]
