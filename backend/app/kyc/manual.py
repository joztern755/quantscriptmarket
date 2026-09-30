"""Manual KYC for the internal phase: no third-party provider. The creator is told an operator will contact them;
an admin records the verdict with POST /v1/admin/users/{id}/kyc (ONE admin decides — owner decision 30 Sep 2026;
an admin cannot approve their own KYC; rejection is immediate). There is no webhook."""
from __future__ import annotations

from typing import Mapping, Optional

from app.errors import ValidationFailed
from app.kyc.base import PENDING, KycEvent, KycSession, KycWebhookError, is_user_id

NAME = "manual"


class ManualProvider:
    name = NAME

    def create_session(self, *, user_id: str, return_url: str) -> KycSession:
        if not is_user_id(user_id):
            raise ValidationFailed("user_id must be a UUID")
        # provider_ref is deterministic so repeated requests reuse the same kyc_creators row
        return KycSession(provider=NAME, provider_ref=f"manual:{user_id}", status=PENDING, url="", manual=True)

    def parse_webhook(self, payload: bytes, headers: Mapping[str, str]) -> Optional[KycEvent]:
        raise KycWebhookError("manual KYC has no webhook")
