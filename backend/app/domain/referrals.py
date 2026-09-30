"""Referrals (SPEC §1.2): tier evaluation, reward from pool, codes, self-referral checks, first-touch window."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections.abc import Iterable, Sequence

from app.config import Economics, ReferralTier
from app.money import BPS

from ._common import require_aware, require_non_negative

__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "REFERRAL_COOKIE_DAYS",
    "evaluate_tier",
    "reward_from_pool",
    "generate_referral_code",
    "normalize_referral_code",
    "ReferralIdentity",
    "self_referral_reasons",
    "is_self_referral",
    "first_touch_valid",
]

#: Upper-case alphanumerics without look-alikes (no 0/O, 1/I/L). 31 symbols → 8 chars ≈ 39.6 bits.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
REFERRAL_COOKIE_DAYS = 30


def evaluate_tier(
    active_users_30d: int,
    notional_30d_micro: int,
    tiers: Sequence[ReferralTier] | None = None,
) -> ReferralTier:
    """Highest tier whose condition is satisfied; a tier is satisfied if EITHER threshold is met
    (active referred users ≥ min_active_users OR referred 30d notional ≥ min_notional_30d_micro).

    "Highest" = largest `share_of_pool_bps` (ties → the later tier in the sequence). The default starter tier
    has 0/0 thresholds and is therefore always satisfied. Raises ValueError if no tier matches (misconfig).
    """
    users = require_non_negative("active_users_30d", active_users_30d)
    notional = require_non_negative("notional_30d_micro", notional_30d_micro)
    tiers = tuple(tiers) if tiers is not None else Economics().referral_tiers
    best: ReferralTier | None = None
    for t in tiers:
        if users >= t.min_active_users or notional >= t.min_notional_30d_micro:
            if best is None or t.share_of_pool_bps >= best.share_of_pool_bps:
                best = t
    if best is None:
        raise ValueError("no referral tier satisfied; tiers misconfigured (need a 0/0 default tier)")
    return best


def reward_from_pool(pool_micro: int, tier: ReferralTier) -> int:
    """Referrer's cut of a referral pool amount (floored; the remainder belongs to the platform)."""
    pool = require_non_negative("pool_micro", pool_micro)
    share = require_non_negative("share_of_pool_bps", tier.share_of_pool_bps)
    if share > BPS:
        raise ValueError("tier share above 100%")
    return (pool * share) // BPS


def generate_referral_code(length: int = CODE_LENGTH) -> str:
    """Random referral code from `secrets` over the unambiguous alphabet. Uniqueness is the DB's job
    (UNIQUE constraint; caller retries on conflict)."""
    if length < 6:
        raise ValueError("referral code too short")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def normalize_referral_code(raw: str | None) -> str | None:
    """Canonicalize a user-supplied `?ref=` value: strip, upper-case, drop dashes/spaces. Returns None if it
    cannot be a valid code (wrong length or characters) — callers then simply ignore the referral."""
    if not isinstance(raw, str):
        return None
    code = raw.strip().upper().replace("-", "").replace(" ", "")
    if len(code) != CODE_LENGTH or any(ch not in CODE_ALPHABET for ch in code):
        return None
    return code


@dataclass(frozen=True)
class ReferralIdentity:
    """What we know about one side of a referral. Empty/None values never match anything."""
    user_id: str
    wallet_addresses: frozenset[str] = field(default_factory=frozenset)
    device_hashes: frozenset[str] = field(default_factory=frozenset)

    @staticmethod
    def of(user_id: str, wallets: Iterable[str | None] = (), devices: Iterable[str | None] = ()) -> "ReferralIdentity":
        return ReferralIdentity(
            user_id=user_id,
            wallet_addresses=frozenset(w.strip().lower() for w in wallets if w and w.strip()),
            device_hashes=frozenset(d.strip() for d in devices if d and d.strip()),
        )


def _wallets(i: ReferralIdentity) -> set[str]:
    return {w.strip().lower() for w in i.wallet_addresses if w and w.strip()}


def _devices(i: ReferralIdentity) -> set[str]:
    return {d.strip() for d in i.device_hashes if d and d.strip()}


def self_referral_reasons(referrer: ReferralIdentity, referee: ReferralIdentity) -> tuple[str, ...]:
    """Reasons ("same_user", "same_wallet", "same_device") the referral is a self-referral; empty if none.
    Wallets compare case-insensitively; device hashes compare exactly. Blank values are ignored."""
    reasons: list[str] = []
    if referrer.user_id and referee.user_id and str(referrer.user_id) == str(referee.user_id):
        reasons.append("same_user")
    if _wallets(referrer) & _wallets(referee):
        reasons.append("same_wallet")
    if _devices(referrer) & _devices(referee):
        reasons.append("same_device")
    return tuple(reasons)


def is_self_referral(referrer: ReferralIdentity, referee: ReferralIdentity) -> bool:
    return bool(self_referral_reasons(referrer, referee))


def first_touch_valid(first_touch_at: datetime, signup_at: datetime, max_age_days: int = REFERRAL_COOKIE_DAYS) -> bool:
    """A stored first-touch `?ref=` binds at signup only if signup happens within the cookie window
    (0 ≤ signup − first_touch ≤ max_age_days). Binding is immutable afterwards (caller enforces)."""
    ft = require_aware("first_touch_at", first_touch_at)
    su = require_aware("signup_at", signup_at)
    age = su - ft
    return timedelta(0) <= age <= timedelta(days=max_age_days)
