"""Provider-agnostic creator-KYC interface (SPEC §3 ``kyc_creators``, §12: creator KYC before listing / paid posts /
payouts). Documents and biometrics stay at the provider; we keep only (provider, provider_ref, status).

Statuses are the DB enum ``kyc_status``: ``pending`` | ``approved`` | ``rejected``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol

from app.errors import AppError

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
STATUSES = (PENDING, APPROVED, REJECTED)
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class KycWebhookError(AppError):
    """Bad signature / malformed payload. 400 — the provider should not retry a forged or broken delivery."""

    http_status, code = 400, "invalid_webhook_signature"


class KycNotConfigured(AppError):
    http_status, code = 503, "service_unavailable"


def is_user_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


@dataclass(frozen=True)
class KycConfig:
    """Read from Settings when config.py carries the fields, else from the environment (same names upper-case):
    KYC_PROVIDER (manual | sumsub; default manual), KYC_APP_TOKEN, KYC_SECRET_KEY, KYC_WEBHOOK_SECRET,
    KYC_LEVEL_NAME. Secrets are never logged or put in ``repr``."""

    provider: str = "manual"
    app_token: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    level_name: str = ""
    api_base: str = "https://api.sumsub.com"
    link_ttl_s: int = 1800
    require_live: bool = False     # prod: ignore provider sandbox-mode notifications
    refetch: bool = True           # re-read the verdict from the provider API before storing it (defence in depth)

    @classmethod
    def from_settings(cls, settings: Any = None) -> "KycConfig":
        def get(name: str, default: str = "") -> str:
            val = getattr(settings, name, None) if settings is not None else None
            if val in (None, ""):
                val = os.environ.get(name.upper(), default)
            return str(val or default).strip()

        return cls(provider=(get("kyc_provider", "manual") or "manual").lower(), app_token=get("kyc_app_token"),
                   secret_key=get("kyc_secret_key"), webhook_secret=get("kyc_webhook_secret"),
                   level_name=get("kyc_level_name"),
                   api_base=get("kyc_api_base", "https://api.sumsub.com") or "https://api.sumsub.com",
                   require_live=bool(getattr(settings, "is_prod", False)))


@dataclass(frozen=True)
class KycSession:
    """What POST /v1/creator/kyc/session needs. ``url`` is the provider's hosted verification page (full-page
    redirect — no iframe, so our CSP/Permissions-Policy stay unchanged); empty when ``manual``."""

    provider: str
    provider_ref: str
    status: str = PENDING
    url: str = ""
    manual: bool = False

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"provider": self.provider, "provider_ref": self.provider_ref, "status": self.status,
                             "url": self.url}
        if self.manual:
            d["manual"] = True
        return d


@dataclass(frozen=True)
class KycEvent:
    """A VERIFIED provider notification, mapped to our status."""

    provider: str
    provider_ref: str            # provider's applicant id
    user_id: str                 # our users.id (the externalUserId we sent)
    event_type: str
    status: str                  # pending | approved | rejected
    final: bool = False          # a final rejection (no resubmission allowed)
    revoke: bool = False         # provider reset/deleted the applicant: an approval no longer stands
    reject_labels: tuple[str, ...] = ()
    event_id: str = ""


class KycProvider(Protocol):
    name: str

    def create_session(self, *, user_id: str, return_url: str) -> KycSession: ...

    def parse_webhook(self, payload: bytes, headers: Mapping[str, str]) -> Optional[KycEvent]: ...


def next_status(current: Optional[str], event: KycEvent) -> Optional[str]:
    """Status to store for ``event`` given the stored one; None = no change.

    * provider verdicts win in both directions (approved → rejected is honoured: ongoing monitoring can revoke);
    * an approval is NOT downgraded to pending by a routine "pending"/"on hold" notification — only by an explicit
      reset/delete (``revoke``), which forces re-verification before any new listing or payout.
    """
    if event.status not in STATUSES:
        return None
    if current == event.status:
        return None
    if current == APPROVED and event.status == PENDING and not event.revoke:
        return None
    return event.status
