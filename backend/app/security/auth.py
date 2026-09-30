"""Firebase ID-token verification + MFA / step-up / admin policy. SPEC §5.2.

Two verifiers, same output (`AuthContext`):
* `PyJwtFirebaseVerifier` — independent, local verification with PyJWT against Google's securetoken x509 certs
  (cached per Cache-Control max-age). Checks: header alg == RS256 only (alg=none / HS256 key-confusion refused),
  kid known, RSA signature, iss == https://securetoken.google.com/<project>, aud == <project>, exp in the future,
  iat and auth_time not in the future, auth_time <= iat, lifetime <= 1h, sub non-empty (<= 128 chars).
* `FirebaseAdminVerifier` — firebase_admin.auth.verify_id_token(check_revoked=True) (import-guarded). Adds
  revocation + disabled-user checks (one Auth backend call per request).
`build_verifier(settings)` returns both chained in prod (cheap local check first, then revocation check).

Policy (decided here, documented in docs/SECURITY.md by the lead):
* Sign-in providers: google.com and apple.com ONLY. Anything else (password, phone, anonymous, custom) -> 401.
* MFA: ONLY `totp` is accepted as a second factor. `phone` (SMS) is refused: SMS is vulnerable to SIM-swap and
  SS7 interception and this account controls trading authority and payouts. SPEC §2 says "TOTP MFA enforced".
* Step-up: auth_time within 300 s AND totp MFA. auth_time is refreshed only by a real sign-in
  (reauthenticateWithPopup/Redirect + MFA), not by the SDK's silent hourly token refresh.
* Admin: role comes from OUR DB via a caller-supplied lookup, never from token custom claims.

Firebase Auth emulator tokens are unsigned (alg=none) and are always refused; there is no bypass flag.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

import jwt
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from app.errors import ExternalServiceError, Forbidden, StepUpRequired, Unauthorized
from app.https_only import https_open
from app.logging import get_logger

try:  # prod only
    import firebase_admin  # type: ignore
    from firebase_admin import auth as _fb_auth  # type: ignore
except Exception:  # noqa: BLE001
    firebase_admin = None
    _fb_auth = None

__all__ = [
    "ALLOWED_PROVIDERS", "ACCEPTED_SECOND_FACTORS", "STEP_UP_MAX_AGE_S", "GOOGLE_SECURETOKEN_CERTS_URL",
    "AuthContext", "TokenVerifier", "CertSource", "GoogleCertCache", "StaticCertSource",
    "PyJwtFirebaseVerifier", "FirebaseAdminVerifier", "ChainedVerifier", "build_verifier",
    "extract_bearer", "require_mfa", "require_step_up", "require_admin",
]

log = get_logger(__name__)

ALLOWED_PROVIDERS: frozenset[str] = frozenset({"google.com", "apple.com"})
ACCEPTED_SECOND_FACTORS: frozenset[str] = frozenset({"totp"})  # NOT "phone" (SMS) — see module docstring
STEP_UP_MAX_AGE_S = 300
GOOGLE_SECURETOKEN_CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)
MAX_TOKEN_LEN = 8192
FIREBASE_ID_TOKEN_MAX_LIFETIME_S = 3600
DEFAULT_LEEWAY_S = 30


@dataclass(frozen=True)
class AuthContext:
    uid: str
    email: str | None
    email_verified: bool
    sign_in_provider: str
    second_factor: str | None     # firebase.sign_in_second_factor, e.g. "totp"; None if no MFA in this session
    auth_time: int                # epoch seconds of the last real sign-in
    claims: Mapping[str, Any] = field(repr=False, compare=False)


class TokenVerifier(Protocol):
    def verify(self, token: str) -> AuthContext: ...


def extract_bearer(authorization: str | None) -> str:
    """'Bearer <jwt>' -> '<jwt>'. Raises Unauthorized on anything else."""
    if not authorization:
        raise Unauthorized("missing bearer token")
    parts = authorization.strip().split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise Unauthorized("malformed authorization header")
    if len(parts[1]) > MAX_TOKEN_LEN:
        raise Unauthorized("token too large")
    return parts[1]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _context_from_claims(claims: Mapping[str, Any], allowed_providers: frozenset[str]) -> AuthContext:
    sub = claims.get("sub")
    if not isinstance(sub, str) or not 0 < len(sub) <= 128:
        raise Unauthorized("invalid token subject")
    if "user_id" in claims and claims["user_id"] != sub:
        raise Unauthorized("token subject mismatch")
    fb = claims.get("firebase")
    if not isinstance(fb, Mapping):
        raise Unauthorized("missing firebase claim")
    if fb.get("tenant"):
        raise Unauthorized("multi-tenant tokens are not accepted")
    provider = fb.get("sign_in_provider")
    if provider not in allowed_providers:
        raise Unauthorized("sign-in provider not allowed", provider=str(provider))
    second = fb.get("sign_in_second_factor")
    if second is not None and not isinstance(second, str):
        raise Unauthorized("malformed second-factor claim")
    auth_time = claims.get("auth_time")
    if not _is_int(auth_time):
        raise Unauthorized("missing auth_time")
    email = claims.get("email")
    return AuthContext(
        uid=sub,
        email=email if isinstance(email, str) else None,
        email_verified=claims.get("email_verified") is True,
        sign_in_provider=provider,
        second_factor=second,
        auth_time=auth_time,
        claims=MappingProxyType(dict(claims)),
    )


# ---------------------------------------------------------------- certificate sources
class CertSource(Protocol):
    def get_key(self, kid: str) -> RSAPublicKey: ...


def _load_rsa_cert(pem: str | bytes) -> RSAPublicKey:
    cert = x509.load_pem_x509_certificate(pem.encode() if isinstance(pem, str) else pem)
    key = cert.public_key()
    if not isinstance(key, RSAPublicKey) or key.key_size < 2048:
        raise ValueError("certificate is not an RSA>=2048 key")
    return key


class StaticCertSource:
    """Fixed {kid: PEM certificate} map — tests and emergencies (pin certs by hand)."""

    def __init__(self, certs: Mapping[str, str | bytes]) -> None:
        self._keys = {kid: _load_rsa_cert(pem) for kid, pem in certs.items()}

    def get_key(self, kid: str) -> RSAPublicKey:
        try:
            return self._keys[kid]
        except KeyError:
            raise Unauthorized("unknown signing key") from None


FetchResult = tuple[int, bytes, str]  # (http status, body, Cache-Control header)


def _urllib_fetch(url: str, timeout_s: float) -> FetchResult:
    if not url.startswith("https://"):
        raise ValueError("cert URL must be https")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "aijalon-api"})  # noqa: S310
    with https_open(req, timeout=timeout_s) as resp:  # https only, TLS verified
        return resp.status, resp.read(1 << 20), resp.headers.get("Cache-Control", "") or ""


_MAX_AGE_RE = re.compile(r"(?:^|[,\s])max-age=(\d+)", re.I)


class GoogleCertCache:
    """Google securetoken x509 certs cached for Cache-Control max-age (clamped to [min_ttl, max_ttl]).

    * Unknown kid -> one forced refresh (at most once per `unknown_kid_refresh_s`), then reject.
    * Refresh failure with an expired cache -> keep serving the stale set for up to `stale_grace_s`
      (availability during a googleapis blip; old keys remain valid for hours after rotation), then fail closed.
    """

    def __init__(self, url: str = GOOGLE_SECURETOKEN_CERTS_URL, *, fetch: Callable[[str, float], FetchResult] | None = None,
                 clock: Callable[[], float] = time.time, timeout_s: float = 5.0, min_ttl_s: int = 60,
                 max_ttl_s: int = 24 * 3600, unknown_kid_refresh_s: int = 60, stale_grace_s: int = 3600) -> None:
        self._url = url
        self._fetch = fetch or _urllib_fetch
        self._clock = clock
        self._timeout = timeout_s
        self._min_ttl, self._max_ttl = min_ttl_s, max_ttl_s
        self._unknown_kid_refresh_s = unknown_kid_refresh_s
        self._stale_grace_s = stale_grace_s
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires_at = 0.0
        self._last_fetch = float("-inf")
        self._lock = threading.Lock()

    def _ttl(self, cache_control: str) -> int:
        cc = cache_control.lower()
        if "no-store" in cc or "no-cache" in cc:
            return self._min_ttl
        m = _MAX_AGE_RE.search(cc)
        ttl = int(m.group(1)) if m else self._min_ttl
        return max(self._min_ttl, min(self._max_ttl, ttl))

    def _refresh_locked(self) -> None:
        now = self._clock()
        self._last_fetch = now
        try:
            status, body, cache_control = self._fetch(self._url, self._timeout)
            if status != 200:
                raise ValueError(f"http {status}")
            data = json.loads(body)
            if not isinstance(data, dict) or not data:
                raise ValueError("empty cert set")
            keys = {str(kid): _load_rsa_cert(pem) for kid, pem in data.items()}
        except Exception as e:  # noqa: BLE001
            if self._keys and now < self._expires_at + self._stale_grace_s:
                log.warning("firebase cert refresh failed; serving cached certs", extra={"fields": {"error": type(e).__name__}})
                return
            raise ExternalServiceError("could not fetch token signing certificates", service="securetoken") from e
        self._keys = keys
        self._expires_at = now + self._ttl(cache_control)

    def get_key(self, kid: str) -> RSAPublicKey:
        with self._lock:
            now = self._clock()
            if now >= self._expires_at:
                self._refresh_locked()
            key = self._keys.get(kid)
            if key is None and now - self._last_fetch >= self._unknown_kid_refresh_s:
                self._refresh_locked()
                key = self._keys.get(kid)
        if key is None:
            raise Unauthorized("unknown signing key")
        return key


# ---------------------------------------------------------------- verifiers
class PyJwtFirebaseVerifier:
    """Independent Firebase ID-token verifier (no firebase_admin). Does NOT check revocation."""

    def __init__(self, project_id: str, certs: CertSource, *, clock: Callable[[], float] = time.time,
                 leeway_s: int = DEFAULT_LEEWAY_S, allowed_providers: frozenset[str] = ALLOWED_PROVIDERS) -> None:
        if not project_id:
            raise ValueError("firebase project id required")
        self._project = project_id
        self._issuer = f"https://securetoken.google.com/{project_id}"
        self._certs = certs
        self._clock = clock
        self._leeway = leeway_s
        self._providers = allowed_providers

    def verify(self, token: str) -> AuthContext:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
            raise Unauthorized("invalid token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Unauthorized("invalid token") from None
        if header.get("alg") != "RS256":
            raise Unauthorized("invalid token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 0 < len(kid) <= 128:
            raise Unauthorized("invalid token key id")
        key = self._certs.get_key(kid)
        try:
            claims = jwt.decode(
                token, key=key, algorithms=["RS256"], audience=self._project, issuer=self._issuer,
                options={
                    "verify_signature": True, "verify_aud": True, "verify_iss": True,
                    # time claims are checked below against the injected clock
                    "verify_exp": False, "verify_iat": False, "verify_nbf": False,
                    "require": ["exp", "iat", "aud", "iss", "sub", "auth_time"],
                },
            )
        except jwt.PyJWTError:
            raise Unauthorized("invalid token") from None
        if claims.get("aud") != self._project or claims.get("iss") != self._issuer:
            raise Unauthorized("invalid token audience/issuer")
        now = int(self._clock())
        exp, iat, auth_time = claims.get("exp"), claims.get("iat"), claims.get("auth_time")
        if not (_is_int(exp) and _is_int(iat) and _is_int(auth_time)):
            raise Unauthorized("invalid token time claims")
        if exp <= now - self._leeway:
            raise Unauthorized("token expired")
        if iat > now + self._leeway or auth_time > now + self._leeway:
            raise Unauthorized("token issued in the future")
        if auth_time > iat + self._leeway:
            raise Unauthorized("auth_time after iat")
        if exp - iat > FIREBASE_ID_TOKEN_MAX_LIFETIME_S + self._leeway or exp <= iat:
            raise Unauthorized("token lifetime invalid")
        return _context_from_claims(claims, self._providers)


class FirebaseAdminVerifier:
    """firebase_admin.auth.verify_id_token(check_revoked=True). Prod revocation / disabled-user check."""

    def __init__(self, project_id: str, *, app: Any = None, allowed_providers: frozenset[str] = ALLOWED_PROVIDERS) -> None:
        if _fb_auth is None:
            raise RuntimeError("firebase-admin is not installed")
        if not project_id:
            raise ValueError("firebase project id required")
        self._project = project_id
        self._app = app
        self._providers = allowed_providers

    def verify(self, token: str) -> AuthContext:
        cert_err = getattr(_fb_auth, "CertificateFetchError", None)
        try:
            claims = _fb_auth.verify_id_token(token, app=self._app, check_revoked=True)
        except Exception as e:  # noqa: BLE001
            if cert_err is not None and isinstance(e, cert_err):
                raise ExternalServiceError("could not fetch token signing certificates", service="firebase") from e
            # RevokedIdTokenError, UserDisabledError, ExpiredIdTokenError, InvalidIdTokenError, ValueError
            raise Unauthorized("invalid or revoked token", reason=type(e).__name__) from None
        if claims.get("aud") != self._project:
            raise Unauthorized("invalid token audience")
        return _context_from_claims(claims, self._providers)


class ChainedVerifier:
    """All verifiers must accept and agree on uid/auth_time; returns the first context."""

    def __init__(self, *verifiers: TokenVerifier) -> None:
        if not verifiers:
            raise ValueError("at least one verifier")
        self._verifiers = verifiers

    def verify(self, token: str) -> AuthContext:
        first = self._verifiers[0].verify(token)
        for v in self._verifiers[1:]:
            other = v.verify(token)
            if other.uid != first.uid or other.auth_time != first.auth_time:
                raise Unauthorized("verifier disagreement")
        return first


def build_verifier(settings: Any = None, *, certs: CertSource | None = None, firebase_app: Any = None) -> TokenVerifier:
    """Prod: PyJWT (local) then firebase_admin (revocation) — firebase-admin is REQUIRED in prod.
    Dev/test: PyJWT verifier only."""
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    local = PyJwtFirebaseVerifier(settings.firebase_project_id, certs or GoogleCertCache())
    if settings.is_prod:
        return ChainedVerifier(local, FirebaseAdminVerifier(settings.firebase_project_id, app=firebase_app))
    return local


# ---------------------------------------------------------------- policy
def _epoch(now: datetime | int | float | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("naive datetime; use UTC-aware")
        return now.timestamp()
    return float(now)


def require_mfa(ctx: AuthContext) -> AuthContext:
    """Every account action (SPEC §5.2). Only TOTP is accepted; SMS/phone is refused."""
    if ctx.second_factor not in ACCEPTED_SECOND_FACTORS:
        raise Unauthorized("mfa_required", reason="mfa_required", second_factor=ctx.second_factor)
    return ctx


def require_step_up(ctx: AuthContext, now: datetime | int | float | None = None, max_age: int = STEP_UP_MAX_AGE_S,
                    *, skew_s: int = DEFAULT_LEEWAY_S) -> AuthContext:
    """Financial/security actions: fresh sign-in (auth_time within max_age seconds) AND TOTP MFA."""
    if ctx.second_factor not in ACCEPTED_SECOND_FACTORS:
        raise StepUpRequired("step-up requires TOTP MFA", reason="mfa_required")
    age = _epoch(now) - ctx.auth_time
    if age > max_age or age < -skew_s:
        raise StepUpRequired("recent sign-in required", reason="stale_auth", max_age=max_age)
    return ctx


def require_admin(ctx: AuthContext, role_lookup: Callable[[str], str | None],
                  now: datetime | int | float | None = None) -> AuthContext:
    """Admin actions: step-up + role == 'admin' from OUR DB (role_lookup(uid) -> role, or None if the user is
    unknown/suspended). Token custom claims are never consulted."""
    require_step_up(ctx, now)
    try:
        role = role_lookup(ctx.uid)
    except Exception as e:  # noqa: BLE001 - fail closed
        raise Forbidden("admin role check failed") from e
    if role != "admin":
        raise Forbidden("admin role required")
    return ctx
