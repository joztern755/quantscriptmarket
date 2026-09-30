"""API dependency layer.

* `Services` is the single container of everything a route needs. Every external collaborator is typed as a
  Protocol ("port") here; concrete implementations live in `app.api.adapters` (which imports the other teams'
  modules lazily) and in-memory fakes in `app.api.testing`.
* FastAPI dependencies: authentication (Firebase ID token + TOTP MFA), user load/create (with first-touch
  referral binding), consent gate, step-up, roles, feature flags, idempotency, rate limits, internal OIDC.

Dependency ladder (each includes the previous):
    current_user      token valid + MFA (TOTP) + user row active + per-user rate limit    → /me, /consents
    consented_user    + current versions of terms/risk/privacy/jurisdiction/waiver       → all other auth routes
    step_up_user      + fresh sign-in (auth_time ≤ 300 s) + MFA                          → SPEC §5.2 actions
    admin_user        + role admin (from DB, never from token claims)
    admin_step_up     + step-up                                                          → all admin mutations
    creator_user      + feature flag + role creator|admin + creator_agreement consent
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
import time
from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional, Protocol

from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api import validation as v
from app.config import Settings
from app.errors import (
    AppError,
    Conflict,
    ConsentRequired,
    Forbidden,
    RateLimited,
    StepUpRequired,
    Unauthorized,
    ValidationFailed,
)
from app.logging import get_logger

log = get_logger("app.api")

STEP_UP_MAX_AGE_SECONDS = 300
SITE_DOCS: tuple[str, ...] = ("terms", "risk", "privacy", "jurisdiction", "waiver")
DEFAULT_LEGAL_VERSIONS: dict[str, str] = {
    "terms": "2026-09-30",
    "risk": "2026-09-30",
    "privacy": "2026-09-30",
    "jurisdiction": "2026-09-30",
    "waiver": "2026-09-30",
    "creator_agreement": "2026-09-30",
    "subscription_ack": "2026-09-30",
}
ALLOWED_SIGN_IN_PROVIDERS = frozenset({"google.com", "apple.com"})


# ============================================================================================================
# Extra errors (API-specific; subclass app.errors so the global handler maps them)
# ============================================================================================================
class JurisdictionBlocked(Forbidden):
    http_status, code = 451, "jurisdiction_restricted"


class MfaRequired(Unauthorized):
    """Token has no TOTP second factor → the web client sends the user to sign-in / MFA enrolment."""
    code = "mfa_required"


class ServiceUnavailable(AppError):
    http_status, code = 503, "service_unavailable"


class NotImplementedYet(AppError):
    http_status, code = 501, "not_implemented"


class PayloadTooLarge(AppError):
    http_status, code = 413, "payload_too_large"


# ============================================================================================================
# API config — values not (yet) in app.config.Settings are read with getattr + safe defaults, in ONE place.
# The lead should add these fields to Settings (env: EDGE_AUTH_SECRET, SCHEDULER_SA_EMAIL, INTERNAL_AUDIENCE,
# SANDBOX_URL, IP_HASH_SALT, KYC_PROVIDER, LEGAL_VERSIONS_JSON).
# ============================================================================================================
@dataclass(frozen=True)
class ApiConfig:
    edge_auth_secret: str
    scheduler_sa_email: str
    internal_audience: str
    sandbox_url: str
    pepper: bytes
    kyc_provider: str
    legal_versions: dict[str, str]
    legal_doc_hashes: dict[str, str]          # doc -> sha256 of the current rendered text ({} = not enforced)
    launch: "LaunchConfig"
    max_body_bytes: int = 128 * 1024
    max_upload_body_bytes: int = 1024 * 1024


@dataclass(frozen=True)
class LaunchConfig:
    """Launch-phase guard rails (owner decision). Read with getattr so the API works before config.py has them;
    missing values fail CLOSED in prod (internal phase, no payouts)."""
    phase: str                                   # "internal" | "public"
    allowlist_emails: frozenset[str]
    max_allocation_per_user_micro: Optional[int]
    max_total_platform_allocation_micro: Optional[int]
    max_user_leverage_x100: Optional[int]
    payouts_enabled: bool

    @property
    def internal(self) -> bool:
        return self.phase != "public"


def _launch(settings: Settings) -> LaunchConfig:
    prod = settings.is_prod
    phase = str(getattr(settings, "launch_phase", "internal" if prod else "public") or "internal").lower()
    emails = getattr(settings, "allowlist_emails", ()) or ()
    if isinstance(emails, str):
        emails = [e for e in emails.split(",")]
    lev = getattr(settings, "max_user_leverage", None)

    def _opt_int(name: str) -> Optional[int]:
        val = getattr(settings, name, None)
        return int(val) if val is not None else None

    return LaunchConfig(
        phase=phase,
        allowlist_emails=frozenset(e.strip().lower() for e in emails if e and e.strip()),
        max_allocation_per_user_micro=_opt_int("max_allocation_per_user_micro"),
        max_total_platform_allocation_micro=_opt_int("max_total_platform_allocation_micro"),
        max_user_leverage_x100=(int(Decimal(str(lev)) * 100) if lev is not None else None),
        payouts_enabled=bool(getattr(settings, "payouts_enabled", not prod)),
    )


def _pepper(settings: Settings) -> bytes:
    """HMAC pepper for ip/user-agent hashes (same secret as the audit log: AUDIT_PEPPER_B64). Dev falls back to
    a fixed non-secret value; prod without a pepper refuses to hash (fail closed at startup, see main)."""
    raw = getattr(settings, "audit_pepper_b64", "") or ""
    if raw:
        try:
            pep = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            pep = b""
        if len(pep) >= 32:
            return pep
    return b"" if settings.is_prod else b"aijalon-dev-pepper-not-secret-000000"


def api_config(settings: Settings) -> ApiConfig:
    legal = dict(DEFAULT_LEGAL_VERSIONS)
    legal.update(getattr(settings, "legal_versions", None) or {})
    return ApiConfig(
        edge_auth_secret=getattr(settings, "edge_auth_secret", "") or "",
        scheduler_sa_email=(getattr(settings, "scheduler_sa_email", "") or "").lower(),
        internal_audience=getattr(settings, "internal_audience", "") or settings.api_origin,
        sandbox_url=(getattr(settings, "sandbox_url", "") or "").rstrip("/"),
        pepper=_pepper(settings),
        kyc_provider=getattr(settings, "kyc_provider", "") or "",
        legal_versions=legal,
        legal_doc_hashes=dict(getattr(settings, "legal_doc_hashes", None) or {}),
        launch=_launch(settings),
    )


# ============================================================================================================
# Ports (Protocols). Adapters in app/api/adapters.py; fakes in app/api/testing.py.
# `conn` is a SQLAlchemy Connection inside an open transaction (Database.begin()).
# ============================================================================================================
class DatabasePort(Protocol):
    def begin(self) -> AbstractContextManager[Any]: ...          # transactional connection (commit on exit)


class AuthPort(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...          # raises Unauthorized
    def require_mfa(self, claims: dict[str, Any]) -> None: ...   # raises Unauthorized/StepUpRequired
    def require_step_up(self, claims: dict[str, Any], max_age_seconds: int) -> None: ...


class AuditPort(Protocol):
    def write(self, conn: Any, *, actor: str, action: str, target: str, payload: dict[str, Any],
              ip_hash: Optional[str]) -> None: ...


class LedgerPort(Protocol):
    def ensure_account(self, conn: Any, code: str) -> None: ...                # kind inferred from the code
    def post(self, conn: Any, *, idempotency_key: str, kind: str, memo: str,
             entries: list[tuple[str, int]], created_by: str) -> str: ...     # returns tx id (idempotent)
    def balance(self, conn: Any, account_code: str) -> int: ...              # raw Σ amount_micro (debit +)


@dataclass(frozen=True)
class SealedAgentKey:
    agent_address: str
    key_ciphertext: bytes
    kms_key_version: str


class AgentKeyPort(Protocol):
    def generate_sealed(self, user_id: str) -> SealedAgentKey: ...   # AAD binds user_id; plaintext never leaves


class TypedDataPort(Protocol):
    """Each returns {"typed_data": <EIP-712 for eth_signTypedData_v4>, "action": <exact /exchange action>, "nonce"}."""
    def approve_agent(self, *, agent_address: str, agent_name: str, nonce: int,
                      signature_chain_id: str) -> dict[str, Any]: ...
    def approve_builder_fee(self, *, builder: str, max_fee_tenths_bp: int, nonce: int,
                            signature_chain_id: str) -> dict[str, Any]: ...
    def usd_send(self, *, destination: str, amount: str, time_ms: int,
                 signature_chain_id: str) -> dict[str, Any]: ...


class HlInfoPort(Protocol):
    def extra_agents(self, user: str) -> list[dict[str, Any]]: ...
    def max_builder_fee(self, user: str, builder: str) -> int: ...  # tenths of a bp
    def clearinghouse_state(self, user: str, dex: str = "") -> dict[str, Any]: ...
    def master_of(self, address: str) -> Optional[str]: ...        # master of a sub-account, else None
    def find_usd_send(self, *, sender: str, destination: str, amount_micro: int, tx_hash: str) -> bool: ...
    def unknown_coins(self, coins: list[str]) -> list[str]: ...


class StripePort(Protocol):
    def create_topup_intent(self, *, user_id: str, amount_micro: int, token: str) -> dict[str, Any]: ...
    def verify_webhook(self, payload: bytes, sig_header: str) -> dict[str, Any]: ...   # raises on bad signature
    def handle_event(self, event: dict[str, Any]) -> Any: ...   # stripe_pay.WebhookOutcome (pure; API applies it)


class UsdcPort(Protocol):
    def build_topup(self, *, master_address: str, amount_micro: int, signature_chain_id: str,
                    time_ms: int) -> dict[str, Any]: ...                       # {typed_data, action, nonce, ...}
    def detect(self, *, senders: list[str], since_ms: Optional[int]) -> list[Any]: ...  # treasury inflows
    def credit_from_detection(self, detection: Any,
                              user_for_address: Callable[[str], Optional[str]]) -> Any: ...  # usdc.UsdcOutcome


class NotifierPort(Protocol):
    def notify(self, conn: Any, *, user_id: Optional[str], severity: str, kind: str,
               payload: dict[str, Any]) -> None: ...
    def notify_alert(self, conn: Any, alert: Any) -> None: ...   # app.alerts.notifier.Alert from other modules


class SandboxPort(Protocol):
    def compile_nocode(self, spec: dict[str, Any]) -> str: ...
    def validate(self, code: str, known_markets: Optional[set[str]]) -> dict[str, Any]: ...  # {ok, errors, meta, code_hash}
    def backtest(self, code: str, meta: dict[str, Any]) -> dict[str, Any]: ...


class CodeVaultPort(Protocol):
    def seal(self, plaintext: bytes, aad: bytes) -> tuple[bytes, str]: ...   # (ciphertext, key_version); encrypt only


class KycPort(Protocol):
    def create_session(self, *, user_id: str, return_url: str) -> dict[str, Any]: ...


class JobsPort(Protocol):
    def run(self, job: str, *, db: DatabasePort, now: datetime, params: dict[str, Any]) -> dict[str, Any]: ...
    def latest_reconciliation(self, conn: Any) -> Optional[dict[str, Any]]: ...


class RateLimitPort(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int) -> bool: ...    # True = allowed


class OidcPort(Protocol):
    def verify(self, token: str, audience: str) -> dict[str, Any]: ...


class WalletSigPort(Protocol):
    def recover(self, message: str, signature: str) -> str: ...  # EIP-191 personal_sign → lower-case address


class DomainPort(Protocol):
    def subscription_split(self, price_micro: int) -> tuple[int, int]: ...        # (creator, platform)
    def post_sale_split(self, price_micro: int) -> tuple[int, int]: ...
    def validate_post_price(self, price_micro: int) -> int: ...
    def plan_price(self, plan: str) -> int: ...
    def plan_allows(self, plan: str, active_count: int) -> bool: ...
    def evaluate_tier(self, active_users: int, notional_micro: int) -> Any: ...
    def generate_referral_code(self) -> str: ...
    def normalize_referral_code(self, raw: Optional[str]) -> Optional[str]: ...
    def self_referral_reasons(self, *, referrer: tuple[str, list[str], list[Optional[str]]],
                              referee: tuple[str, list[str], list[Optional[str]]]) -> tuple[str, ...]: ...
    def track_record(self, *, live_since: Optional[datetime], events: list[dict], spans: list[dict], now: datetime,
                     period_start: Optional[datetime], min_subscribers: int) -> dict[str, Any]: ...
    def estimate_monthly_need(self, prices: Iterable[int], plan_price: int) -> int: ...
    def add_months(self, dt: datetime, months: int) -> datetime: ...


@dataclass
class Services:
    settings: Settings
    db: DatabasePort
    store: Any                       # app.api.store.SqlStore (API-owned SQL); fakes duck-type it
    auth: AuthPort
    audit: AuditPort
    ledger: LedgerPort
    agent_keys: AgentKeyPort
    typed_data: TypedDataPort
    hl: HlInfoPort
    stripe: StripePort
    usdc: UsdcPort
    notifier: NotifierPort
    sandbox: SandboxPort
    code_vault: CodeVaultPort
    kyc: KycPort
    jobs: JobsPort
    ratelimit: RateLimitPort
    oidc: OidcPort
    wallet_sig: WalletSigPort
    domain: DomainPort
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    @property
    def config(self) -> ApiConfig:
        return api_config(self.settings)

    def now(self) -> datetime:
        return self.clock()


def get_services(request: Request) -> Services:
    return request.app.state.services


# ============================================================================================================
# Request context helpers
# ============================================================================================================
def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def client_ip(request: Request) -> Optional[str]:
    """CF-Connecting-IP only when the request is proven to come through Cloudflare (see middleware)."""
    if getattr(request.state, "edge_trusted", False):
        ip = request.headers.get("cf-connecting-ip")
        if ip:
            return ip.strip()[:64]
    return request.client.host if request.client else None


def edge_country(request: Request) -> Optional[str]:
    return getattr(request.state, "edge_country", None)


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    scheme, _, token = h.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 8192:
        raise Unauthorized("missing bearer token")
    return token.strip()


@dataclass
class AuthCtx:
    user: dict[str, Any]
    claims: dict[str, Any]
    ip_hash: Optional[str]
    ua_hash: Optional[str]
    request_id: str
    country: Optional[str]

    @property
    def user_id(self) -> str:
        return str(self.user["id"])

    @property
    def role(self) -> str:
        return str(self.user.get("role") or "user")

    @property
    def actor(self) -> str:
        """Audit actor: 'admin:<uuid>' for admins, else 'user:<uuid>'."""
        return f"{'admin' if self.role == 'admin' else 'user'}:{self.user_id}"


# ----------------------------------------------------------------------------------------------- MFA / step-up
def check_mfa_claims(claims: dict[str, Any]) -> None:
    """Defence in depth (also enforced by app.security.auth): TOTP second factor + Google/Apple provider."""
    fb = claims.get("firebase") or {}
    if fb.get("sign_in_second_factor") != "totp":
        raise MfaRequired("TOTP multi-factor sign-in required")
    if fb.get("sign_in_provider") not in ALLOWED_SIGN_IN_PROVIDERS:
        raise Unauthorized("unsupported sign-in provider")


def check_step_up_claims(claims: dict[str, Any], now: datetime, max_age: int = STEP_UP_MAX_AGE_SECONDS) -> None:
    check_mfa_claims(claims)
    try:
        auth_time = int(claims.get("auth_time"))
    except (TypeError, ValueError):
        raise StepUpRequired("fresh sign-in required") from None
    age = int(now.timestamp()) - auth_time
    if age > max_age or age < -60:
        raise StepUpRequired("fresh sign-in required", max_age_seconds=max_age)


# ----------------------------------------------------------------------------------------------- new-country cache
class _SeenCache:
    """Tiny LRU so the login-country bookkeeping hits the DB once per (user, country) per instance."""

    def __init__(self, size: int = 50_000) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return False
            self._d[key] = None
            if len(self._d) > self._size:
                self._d.popitem(last=False)
            return True


_seen_countries = _SeenCache()


def _referral_code_from(request: Request, claims: dict[str, Any], svc: Services) -> Optional[str]:
    """First-touch referral: a signed `ref` custom claim wins; else the X-Ref-Code header. Only used at creation."""
    raw = claims.get("ref") if isinstance(claims.get("ref"), str) else None
    if raw is None:
        raw = request.headers.get("x-ref-code")
    return svc.domain.normalize_referral_code(raw)


def _create_user(conn: Any, svc: Services, request: Request, claims: dict[str, Any], ip_hash: Optional[str]) -> dict:
    uid = str(claims["uid"])
    referrer_id: Optional[str] = None
    code = _referral_code_from(request, claims, svc)
    if code:
        ref = svc.store.get_user_by_referral_code(conn, code)
        if ref and ref.get("status") == "active":
            referrer_id = str(ref["id"])
    user = None
    for _ in range(5):  # referral code collision → retry with a fresh code
        user = svc.store.create_user(
            conn,
            firebase_uid=uid,
            email=(claims.get("email") or None),
            display_name=(claims.get("name") or None),
            referral_code=svc.domain.generate_referral_code(),
            referred_by=referrer_id,
            mfa_enrolled=True,
        )
        if user is not None:
            break
    if user is None:
        raise Conflict("could not allocate referral code")
    if user.get("_created"):
        svc.audit.write(conn, actor=f"user:{user['id']}", action="user.created", target=f"user:{user['id']}",
                        payload={"referred_by": referrer_id}, ip_hash=ip_hash)
    return user


def enforce_launch_allowlist(launch: LaunchConfig, claims: dict[str, Any]) -> None:
    """Internal launch phase: only allow-listed, verified emails may create an account or sign in."""
    if not launch.internal:
        return
    email = str(claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") is not True or email not in launch.allowlist_emails:
        raise Forbidden("aijalon.trade is in private testing; this account is not on the allowlist",
                        reason="not_allowlisted")


def require_payouts_enabled(svc: "Services") -> None:
    if not svc.config.launch.payouts_enabled:
        raise Forbidden("withdrawals and payouts are not enabled yet", reason="payouts_disabled")


def current_user(request: Request, svc: Services = Depends(get_services)) -> AuthCtx:
    token = _bearer(request)
    claims = svc.auth.verify(token)
    try:
        svc.auth.require_mfa(claims)
    except StepUpRequired:
        raise
    except Unauthorized as e:
        raise MfaRequired(e.message) from None
    check_mfa_claims(claims)
    uid = claims.get("uid") or claims.get("user_id") or claims.get("sub")
    if not uid:
        raise Unauthorized("token has no subject")
    claims = {**claims, "uid": str(uid)}
    # Per-user limit keyed on the verified token subject, BEFORE touching the database.
    if not svc.ratelimit.hit(f"user:{claims['uid']}", 300, 60):
        raise RateLimited("too many requests", retry_after_seconds=60)
    cfg = svc.config
    enforce_launch_allowlist(cfg.launch, claims)
    ip_hash = v.hash_identifier(client_ip(request), cfg.pepper, domain="ip")
    ua_hash = v.hash_identifier(request.headers.get("user-agent"), cfg.pepper, domain="ua")
    country = edge_country(request)
    with svc.db.begin() as conn:
        user = svc.store.get_user_by_firebase_uid(conn, claims["uid"])
        if user is None:
            user = _create_user(conn, svc, request, claims, ip_hash)
        if user.get("status") != "active":
            raise Forbidden("account is not active", status=user.get("status"))
        if country and _seen_countries.add(f"{user['id']}:{country}"):
            if svc.store.record_login_country(conn, str(user["id"]), country):
                svc.notifier.notify(conn, user_id=str(user["id"]), severity="warn", kind="login_new_country",
                                    payload={"country": country})
                svc.audit.write(conn, actor=f"user:{user['id']}", action="auth.new_country",
                                target=f"user:{user['id']}", payload={"country": country}, ip_hash=ip_hash)
    ctx = AuthCtx(user=user, claims=claims, ip_hash=ip_hash, ua_hash=ua_hash, request_id=request_id(request),
                  country=country)
    request.state.user_id = ctx.user_id
    return ctx


def missing_consents(conn: Any, svc: Services, user_id: str, docs: Iterable[str] = SITE_DOCS) -> list[str]:
    versions = svc.config.legal_versions
    accepted = svc.store.accepted_consents(conn, user_id)  # {doc: latest accepted version}
    return [d for d in docs if accepted.get(d) != versions[d]]


def consented_user(ctx: AuthCtx = Depends(current_user), svc: Services = Depends(get_services)) -> AuthCtx:
    with svc.db.begin() as conn:
        missing = missing_consents(conn, svc, ctx.user_id)
    if missing:
        raise ConsentRequired("accept the current legal documents first", missing=missing)
    # The jurisdiction attestation itself is a required consent (above). A residence country, when the user
    # stated one, must not be restricted; the network-level CF-IPCountry check runs in EdgeGuardMiddleware.
    attested = ctx.user.get("country_attested")
    if attested and attested in svc.settings.restricted_countries:
        raise JurisdictionBlocked("service not available in your jurisdiction")
    return ctx


def step_up_user(ctx: AuthCtx = Depends(consented_user), svc: Services = Depends(get_services)) -> AuthCtx:
    svc.auth.require_step_up(ctx.claims, STEP_UP_MAX_AGE_SECONDS)
    check_step_up_claims(ctx.claims, svc.now())
    return ctx


def admin_user(ctx: AuthCtx = Depends(consented_user)) -> AuthCtx:
    if ctx.role != "admin":
        raise Forbidden("admin only")
    return ctx


def admin_step_up(ctx: AuthCtx = Depends(admin_user), svc: Services = Depends(get_services)) -> AuthCtx:
    svc.auth.require_step_up(ctx.claims, STEP_UP_MAX_AGE_SECONDS)
    check_step_up_claims(ctx.claims, svc.now())
    return ctx


def creator_feature(svc: Services = Depends(get_services)) -> None:
    if not svc.settings.feature_creator_uploads:
        raise Forbidden("creator uploads are disabled")


def creator_user(_: None = Depends(creator_feature), ctx: AuthCtx = Depends(consented_user),
                 svc: Services = Depends(get_services)) -> AuthCtx:
    if ctx.role not in ("creator", "admin"):
        raise Forbidden("accept the creator agreement first")
    with svc.db.begin() as conn:
        if missing_consents(conn, svc, ctx.user_id, ("creator_agreement",)):
            raise ConsentRequired("accept the current creator agreement", missing=["creator_agreement"])
    return ctx


def creator_step_up(ctx: AuthCtx = Depends(creator_user), svc: Services = Depends(get_services)) -> AuthCtx:
    svc.auth.require_step_up(ctx.claims, STEP_UP_MAX_AGE_SECONDS)
    check_step_up_claims(ctx.claims, svc.now())
    return ctx


# ============================================================================================================
# Rate limits
# ============================================================================================================
def user_limit(bucket: str, limit: int, window_seconds: int):
    """Per-user bucket (auth'd routes)."""
    def dep(ctx: AuthCtx = Depends(current_user), svc: Services = Depends(get_services)) -> None:
        if not svc.ratelimit.hit(f"{bucket}:u:{ctx.user_id}", limit, window_seconds):
            raise RateLimited("too many requests", retry_after_seconds=window_seconds)
    return Depends(dep)


def ip_limit(bucket: str, limit: int, window_seconds: int):
    """Per-IP bucket (public routes). IP is CF-Connecting-IP only when the edge is authenticated."""
    def dep(request: Request, svc: Services = Depends(get_services)) -> None:
        ip = client_ip(request) or "unknown"
        if not svc.ratelimit.hit(f"{bucket}:ip:{ip}", limit, window_seconds):
            raise RateLimited("too many requests", retry_after_seconds=window_seconds)
    return Depends(dep)


# ============================================================================================================
# Idempotency (required on every POST that moves money)
# ============================================================================================================
def idempotency_key(idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> str:
    if not idempotency_key:
        raise ValidationFailed("Idempotency-Key header is required for this request")
    if not v.IDEMPOTENCY_KEY_RE.match(idempotency_key):
        raise ValidationFailed("Idempotency-Key must be 16–128 chars of [A-Za-z0-9_-:.]")
    return idempotency_key


def payload_fingerprint(scope: str, payload: Any) -> str:
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="json")
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return v.request_fingerprint("POST", scope, body)


def run_idempotent(
    svc: Services,
    *,
    user_id: str,
    key: str,
    scope: str,
    payload: Any,
    work: Callable[[Any], BaseModel],
    status_code: int = 200,
) -> JSONResponse:
    """Run `work(conn)` exactly once per (user, key) inside ONE DB transaction together with the key claim.

    Concurrency: the claim is `INSERT … ON CONFLICT DO NOTHING`; a concurrent request with the same key blocks
    on the unique index until the first commits (then replays) or rolls back (then runs). If `work` raises, the
    whole transaction (including the claim) rolls back, so the client may retry with the same key.
    """
    fp = payload_fingerprint(scope, payload)
    with svc.db.begin() as conn:
        existing = svc.store.idem_claim(conn, user_id=user_id, key=key, scope=scope, fingerprint=fp)
        if existing is not None:
            if existing.get("fingerprint") != fp or existing.get("scope") != scope:
                raise Conflict("Idempotency-Key was already used with a different request")
            if existing.get("response") is None:
                raise Conflict("a request with this Idempotency-Key is still in progress")
            resp = JSONResponse(status_code=int(existing["status_code"]), content=existing["response"])
            resp.headers["Idempotent-Replayed"] = "true"
            return resp
        result = work(conn)
        body = result.model_dump(mode="json")
        svc.store.idem_complete(conn, user_id=user_id, key=key, status_code=status_code, response=body)
    return JSONResponse(status_code=status_code, content=body)


# ============================================================================================================
# Internal (Cloud Scheduler OIDC)
# ============================================================================================================
def scheduler_auth(request: Request, svc: Services = Depends(get_services)) -> dict[str, Any]:
    """Only Google-signed OIDC tokens minted for our audience AND the scheduler service account pass."""
    cfg = svc.config
    if not cfg.scheduler_sa_email:
        raise Unauthorized("internal auth not configured")
    token = _bearer(request)
    claims = svc.oidc.verify(token, cfg.internal_audience)
    email = str(claims.get("email") or "").lower()
    if email != cfg.scheduler_sa_email or claims.get("email_verified") is not True:
        raise Forbidden("caller not allowed")
    return claims


# ============================================================================================================
# Small shared helpers for routers
# ============================================================================================================
def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fee_balance_account(user_id: str) -> str:
    return f"user:{user_id}:fee_balance"


def user_fee_balance(conn: Any, svc: Services, user_id: str) -> int:
    """Fee balance (liability, so user-facing balance = −Σ debit-positive amounts)."""
    return -svc.ledger.balance(conn, fee_balance_account(user_id))


def page_limit(limit: int) -> int:
    return max(1, min(int(limit), 100))


def decode_cursor_or_422(cursor: Optional[str]) -> Optional[tuple[datetime, str]]:
    try:
        return v.decode_cursor(cursor)
    except v.InputError as e:
        raise ValidationFailed(str(e)) from None


def next_cursor(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Store methods fetch limit+1 rows; trim and emit the cursor of the last returned row."""
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        return rows, v.encode_cursor(last["created_at"], str(last["id"]))
    return rows, None


def monotonic_ms() -> int:
    return int(time.time() * 1000)
