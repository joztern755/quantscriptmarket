"""Self-referral controls (REVIEW_AUTH_API F7 / REVIEW_MONEY M2; SPEC §1.2). No FastAPI imports.

Identity of each side = verified wallets + device hashes (user_devices, users.device_fp_hash written at sign-up from
the peppered X-Device-Id / user-agent key) + sign-in networks of the last 30 days (user_ip_nets, /24 IPv4 or /64
IPv6, peppered). app.domain.referrals decides:
  * same wallet / same device → the binding is REFUSED (at account creation from X-Ref-Code or the signed `ref`
    claim, and at PATCH /me);
  * same network only → the binding stands but the referee is FLAGGED (users.referral_flagged_at): no referral reward
    is paid from that referee until ops clears the flag (execution.pg.PgReferralLookup), plus an ops alert;
  * at wallet verification a match flags the referee the same way (the binding is immutable, SPEC §1.2).
Referral rewards and tier counts also require a real paid activity of the referee (SQL user_has_paid_activity, 0011)
and an ACTIVE referrer (suspended referrers earn nothing).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable, Mapping, Optional

from app.domain import referrals as dom

NETWORK_WINDOW = timedelta(days=30)

__all__ = ["identity", "self_referral_check", "flag_self_referral", "NETWORK_WINDOW"]


def identity(conn: Any, svc: Any, user: Mapping[str, Any], *, extra_devices: Iterable[Optional[str]] = (),
             extra_networks: Iterable[Optional[str]] = (), extra_wallets: Iterable[Optional[str]] = ()
             ) -> dom.ReferralIdentity:
    uid = str(user["id"]) if user.get("id") else ""
    wallets: list[Optional[str]] = list(extra_wallets)
    devices: list[Optional[str]] = list(extra_devices)
    networks: list[Optional[str]] = list(extra_networks)
    if uid:
        getter = getattr(svc.store, "referral_identity", None)
        if getter is not None:
            ident = getter(conn, uid, svc.now() - NETWORK_WINDOW)
            wallets += ident.get("wallets") or []
            devices += ident.get("devices") or []
            networks += ident.get("networks") or []
        else:   # minimal fakes
            wallets += [w["address"] for w in svc.store.list_wallets(conn, uid)]
            devices.append(user.get("device_fp_hash"))
    return dom.ReferralIdentity.of(uid, wallets=wallets, devices=devices, networks=networks)


def self_referral_check(conn: Any, svc: Any, *, referrer: Mapping[str, Any], referee: Mapping[str, Any],
                        referee_devices: Iterable[Optional[str]] = (), referee_networks: Iterable[Optional[str]] = (),
                        referee_wallets: Iterable[Optional[str]] = ()) -> tuple[bool, tuple[str, ...]]:
    """(blocked, reasons). ``referee`` may be a not-yet-created user ({"id": None}) with request-derived hashes."""
    a = identity(conn, svc, referrer)
    b = identity(conn, svc, referee, extra_devices=referee_devices, extra_networks=referee_networks,
                 extra_wallets=referee_wallets)
    reasons = dom.self_referral_reasons(a, b)
    return dom.blocking(reasons), reasons


def flag_self_referral(conn: Any, svc: Any, *, referee_id: str, referrer_id: str, reasons: Iterable[str],
                       where: str) -> None:
    reasons = [str(r) for r in reasons]
    flag = getattr(svc.store, "flag_referral", None)
    if flag is not None:
        flag(conn, referee_id, f"self_referral_suspected:{where}:{','.join(reasons)}", svc.now())
    svc.notifier.notify(conn, user_id=None, severity="warn", kind="self_referral_suspected",
                        payload={"user_id": referee_id, "referrer": referrer_id, "reasons": reasons, "where": where},
                        dedup_key=f"self_referral_suspected:{referee_id}:{where}")
