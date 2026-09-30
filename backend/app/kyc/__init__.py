"""Creator KYC (SPEC §12: required before a creator strategy is listed, before paid posts and before payouts).

    create_session(user_id=, return_url=) -> {"provider", "provider_ref", "status", "url"[, "manual": True]}
    parse_webhook(payload, headers)       -> KycEvent | None     (verified; None = nothing to act on)
    next_status(current, event)           -> new status | None

Provider is chosen by KYC_PROVIDER: ``manual`` (default; internal phase — ONE admin records the verdict via
POST /v1/admin/users/{id}/kyc) or ``sumsub`` (hosted verification link + signed webhook POST /v1/webhooks/kyc; a
GREEN verdict becomes ``provider_approved`` and ONE admin confirms it → ``approved``; never auto-approved). Config keys (Settings fields when present, else env): KYC_PROVIDER, KYC_APP_TOKEN,
KYC_SECRET_KEY, KYC_WEBHOOK_SECRET, KYC_LEVEL_NAME (optional KYC_API_BASE).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from app.kyc.base import (
    APPROVED,
    PENDING,
    PROVIDER_APPROVED,
    REJECTED,
    STATUSES,
    STORED_STATUSES,
    KycConfig,
    KycEvent,
    KycNotConfigured,
    KycProvider,
    KycSession,
    KycWebhookError,
    next_status,
)

__all__ = ["APPROVED", "PENDING", "PROVIDER_APPROVED", "REJECTED", "STATUSES", "STORED_STATUSES", "KycConfig", "KycEvent", "KycNotConfigured",
           "KycProvider", "KycSession", "KycWebhookError", "next_status", "get_provider", "create_session",
           "parse_webhook"]


def get_provider(settings: Any = None, *, transport: Any = None) -> KycProvider:
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    cfg = KycConfig.from_settings(settings)
    if cfg.provider == "manual":
        from app.kyc.manual import ManualProvider
        return ManualProvider()
    if cfg.provider == "sumsub":
        from app.kyc.sumsub import SumsubProvider
        return SumsubProvider(cfg, transport=transport)
    raise KycNotConfigured("unknown KYC provider", provider=cfg.provider[:32])


def create_session(*, user_id: str, return_url: str, settings: Any = None,
                   provider: Optional[KycProvider] = None) -> dict[str, Any]:
    """Contract used by app.api.adapters.KycAdapter. ``url`` is empty and ``manual`` is True for manual KYC."""
    p = provider or get_provider(settings)
    return p.create_session(user_id=user_id, return_url=return_url).as_dict()


def parse_webhook(payload: bytes, headers: Mapping[str, str], *, settings: Any = None,
                  provider: Optional[KycProvider] = None) -> Optional[KycEvent]:
    p = provider or get_provider(settings)
    return p.parse_webhook(payload, headers)
