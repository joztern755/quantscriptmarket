"""Sign-in security events (SPEC §5.5 "login from new country, MFA reset"; §12 mandatory alerts ``new_device_login``*
and ``mfa_changed``*). Called by ``deps.current_user`` inside its transaction; no FastAPI imports (unit-testable).

* New country: ``user_login_countries`` (0004). The FIRST country ever seen is recorded silently; a later new country
  → ``new_device_login`` {country, reason: "new_country"}.
* New device: ``user_devices`` (0008) keyed by ``device_hash`` = HMAC(pepper, "device" ‖ key) where key = the web's
  ``X-Device-Id`` header (random id kept in the browser's storage; 16–128 chars ``[A-Za-z0-9_-]``) or, when absent,
  the User-Agent with every digit / dot / underscore removed (browser family + OS, stable across version updates).
  The first device is recorded silently; a later new one → ``new_device_login`` {device: coarse label, country?,
  reason: "new_device"}. A request that is both a new country and a new device raises ONE alert.
* MFA change: Firebase ID tokens of an MFA sign-in carry ``firebase.second_factor_identifier`` (the enrolled
  factor's id). Its HMAC is kept in ``users.mfa_factor_hash`` (0008); the first one is stored silently, a different
  one → ``mfa_changed`` + audit ``auth.mfa_changed``. Tokens without the claim are ignored (never an alert).
* Security hold (REVIEW_AUTH_API F5): a new device / new country sign-in or an MFA change puts the account on a
  48 h hold (``users.security_hold_until``, 0011): no withdrawal or payout request, and no second approval of one,
  until it passes (app.api.billing_ops.require_no_payout_hold).
* Sign-in network (F7): ``network_hash`` = HMAC(pepper, "ipnet" ‖ IPv4 /24 or IPv6 /64 prefix) → user_ip_nets, used
  by the self-referral heuristics (same network as the referrer → the referee is flagged, no referral reward).
Raw device ids / factor ids / IPs are never stored or logged — only peppered hashes.
"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from app.api import validation as v

__all__ = ["device_key", "device_label", "mfa_factor_hash", "network_hash", "record_sign_in", "DEVICE_ID_RE",
           "SECURITY_HOLD"]

SECURITY_HOLD = timedelta(hours=48)

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_VERSIONS = re.compile(r"[0-9._]+")


def device_label(user_agent: Optional[str]) -> Optional[str]:
    """Coarse, non-identifying label for the alert text, e.g. "Chrome on macOS"."""
    ua = str(user_agent or "")
    if not ua:
        return None
    if "Edg/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua or "FxiOS" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "a browser"
    if "iPhone" in ua or "iPad" in ua or "iOS" in ua:
        os_ = "iOS"
    elif "Android" in ua:
        os_ = "Android"
    elif "Windows" in ua:
        os_ = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_ = "macOS"
    elif "Linux" in ua or "X11" in ua:
        os_ = "Linux"
    else:
        os_ = "an unknown system"
    return f"{browser} on {os_}"


def device_key(headers: Mapping[str, str], pepper: bytes | str) -> tuple[Optional[str], Optional[str]]:
    """(device_hash, label) from request headers (lower-case keys); (None, None) when nothing identifies a device."""
    ua = headers.get("user-agent") or ""
    raw = (headers.get("x-device-id") or "").strip()
    if raw and DEVICE_ID_RE.fullmatch(raw):
        key = "id:" + raw
    elif ua.strip():
        key = "ua:" + _VERSIONS.sub("", ua.strip())[:512]
    else:
        return None, None
    return v.hash_identifier(key, pepper, domain="device"), device_label(ua)


def network_hash(ip: Optional[str], pepper: bytes | str) -> Optional[str]:
    """Peppered hash of the sign-in network: IPv4 /24, IPv6 /64 (IPv4-mapped IPv6 → IPv4). None for no/invalid IP."""
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    prefix = 24 if isinstance(addr, ipaddress.IPv4Address) else 64
    net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    return v.hash_identifier(str(net), pepper, domain="ipnet")


def _hold(conn: Any, svc: Any, uid: str) -> None:
    """48 h money-out hold after a security event (F5). The API store always has it; minimal fakes may not."""
    extend = getattr(svc.store, "extend_security_hold", None)
    if extend is None:
        return
    clock = getattr(svc, "now", None)
    now = clock() if callable(clock) else datetime.now(timezone.utc)
    extend(conn, uid, now + SECURITY_HOLD)


def mfa_factor_hash(claims: Mapping[str, Any], pepper: bytes | str) -> Optional[str]:
    fb = claims.get("firebase") or {}
    fid = fb.get("second_factor_identifier") if isinstance(fb, Mapping) else None
    if not isinstance(fid, str) or not fid.strip() or len(fid) > 256:
        return None
    return v.hash_identifier(fid.strip(), pepper, domain="mfa")


def record_sign_in(conn: Any, svc: Any, *, user: Mapping[str, Any], claims: Mapping[str, Any],
                   country: Optional[str], device_hash: Optional[str], device_label_: Optional[str],
                   ip_hash: Optional[str], check_country: bool = True, check_device: bool = True) -> list[str]:
    """Record the sign-in facts and raise the security alerts (see module doc). Returns the alert kinds raised.
    ``check_country`` / ``check_device``: False when the caller's per-process cache already saw that pair."""
    uid = str(user["id"])
    actor = f"user:{uid}"
    raised: list[str] = []
    new_country = bool(check_country and country and svc.store.record_login_country(conn, uid, country))
    new_device = False
    if check_device and device_hash:
        new_device = bool(svc.store.record_device(conn, uid, device_hash, device_label_))
    if new_country or new_device:
        payload: dict[str, Any] = {"reason": "new_device" if new_device else "new_country"}
        if country:
            payload["country"] = country
        if new_device and device_label_:
            payload["device"] = device_label_
        svc.notifier.notify(conn, user_id=uid, severity="warn", kind="new_device_login", payload=payload)
        svc.audit.write(conn, actor=actor, action="auth.new_device" if new_device else "auth.new_country",
                        target=actor, payload={k: val for k, val in payload.items() if k != "reason"},
                        ip_hash=ip_hash)
        raised.append("new_device_login")
        _hold(conn, svc, uid)

    fh = mfa_factor_hash(claims, svc.config.pepper)
    if fh is not None:
        stored = user.get("mfa_factor_hash")
        if stored != fh:
            svc.store.set_mfa_factor_hash(conn, uid, fh)
            if stored:
                svc.notifier.notify(conn, user_id=uid, severity="warn", kind="mfa_changed",
                                    payload={"change": "a different second factor was used to sign in"})
                svc.audit.write(conn, actor=actor, action="auth.mfa_changed", target=actor, payload={},
                                ip_hash=ip_hash)
                raised.append("mfa_changed")
                _hold(conn, svc, uid)
    return raised
