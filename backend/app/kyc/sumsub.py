"""Sumsub-style KYC provider: HMAC-signed REST calls, applicant per creator, hosted WebSDK link, signed webhooks.

Written from Sumsub's public API documentation as last known; NOTHING here was exercised against the live API
(offline build). Every provider-specific detail is marked ``[VERIFY]`` — check each against the Sumsub sandbox
(test app token) before the first real creator, and pin the dashboard settings noted below.

Request signing [VERIFY]
  X-App-Token: <app token>; X-App-Access-Ts: unix seconds;
  X-App-Access-Sig: hex(HMAC-SHA256(secret key, ts + METHOD + path?query + body_bytes)).
Endpoints [VERIFY]
  POST /resources/applicants?levelName=<level>            body {"externalUserId": <our users.id>}  (409 = exists)
  GET  /resources/applicants/-;externalUserId=<id>/one     existing applicant by our id
  GET  /resources/applicants/<applicantId>/one             applicant incl. ``review`` (webhook re-fetch)
  POST /resources/sdkIntegrations/levels/-/websdkLink      body {levelName, userId, ttlInSecs, redirect} -> {url}
  POST /resources/accessTokens/sdk                         body {userId, levelName, ttlInSecs} -> {token}
Webhook [VERIFY]
  Header X-Payload-Digest = hex HMAC of the RAW body with the webhook secret; X-Payload-Digest-Alg names the hash.
  Set the dashboard webhook to HMAC_SHA256_HEX (SHA-1 is refused here). No timestamp is signed, so a captured
  delivery could be replayed: the verdict is therefore RE-READ from the API (``refetch``) before it is stored,
  and applying a verdict is idempotent.
  Body: {type, applicantId, externalUserId, correlationId, levelName, sandboxMode, reviewStatus,
         reviewResult: {reviewAnswer: GREEN|RED, reviewRejectType: FINAL|RETRY, rejectLabels: [...]}}.
Data minimisation: only our opaque user UUID is sent; the creator enters documents on Sumsub's hosted page.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional

from app.errors import ExternalServiceError, ValidationFailed
from app.https_only import https_open
from app.kyc.base import (
    APPROVED,
    PENDING,
    REJECTED,
    KycConfig,
    KycEvent,
    KycNotConfigured,
    KycSession,
    KycWebhookError,
    is_user_id,
)
from app.logging import get_logger

log = get_logger("app.kyc.sumsub")

NAME = "sumsub"
MAX_BODY = 1 << 20
DIGEST_ALGS: dict[str, Any] = {"HMAC_SHA256_HEX": hashlib.sha256, "HMAC_SHA512_HEX": hashlib.sha512}  # [VERIFY]
REVOKE_TYPES = ("applicantReset", "applicantDeleted", "applicantDeactivated")                          # [VERIFY]

#: transport(method, url, headers, body) -> (status, body bytes). Injected in tests; default is urllib.
Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, bytes]"]


def _urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float = 15.0
                      ) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 (https base only)
    try:
        with https_open(req, timeout=timeout) as resp:  # https only (api_base is checked in __init__ too)
            return resp.status, resp.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        return e.code, e.read(MAX_BODY) if e.fp else b""
    except Exception as e:
        raise ExternalServiceError("KYC provider unreachable", error=type(e).__name__) from None


def sign_request(secret_key: str, ts: str, method: str, path_q: str, body: bytes = b"") -> str:
    """[VERIFY] Sumsub request signature: hex(HMAC-SHA256(secret, ts + METHOD + path?query + body))."""
    msg = ts.encode("ascii") + method.upper().encode("ascii") + path_q.encode("utf-8") + (body or b"")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def payload_digest(secret: str, payload: bytes, alg: str = "HMAC_SHA256_HEX") -> str:
    fn = DIGEST_ALGS.get(alg)
    if fn is None:
        raise KycWebhookError("unsupported payload digest algorithm")
    return hmac.new(secret.encode("utf-8"), payload, fn).hexdigest()


def map_review(event_type: str, review: Mapping[str, Any]) -> tuple[str, bool, bool, tuple[str, ...]]:
    """(status, final, revoke, reject_labels) from a Sumsub event type + review block [VERIFY enum values].

    completed + GREEN → approved; completed + RED + FINAL → rejected (final); completed + RED + RETRY → pending
    (the creator may resubmit); anything else (init, pending, queued, onHold, prechecked) → pending.
    Reset / deleted / deactivated applicants → pending with ``revoke`` (an approval no longer stands)."""
    if event_type in REVOKE_TYPES:
        return PENDING, False, True, ()
    status = str(review.get("reviewStatus") or "")
    result = review.get("reviewResult") or {}
    if not isinstance(result, Mapping):
        result = {}
    answer = str(result.get("reviewAnswer") or "").upper()
    reject_type = str(result.get("reviewRejectType") or "").upper()
    labels_raw = result.get("rejectLabels") or ()
    labels = tuple(str(x)[:64] for x in labels_raw[:20]) if isinstance(labels_raw, (list, tuple)) else ()
    if status != "completed":
        return PENDING, False, False, ()
    if answer == "GREEN":
        return APPROVED, False, False, ()
    if answer == "RED":
        if reject_type == "RETRY":
            return PENDING, False, False, labels
        return REJECTED, True, False, labels   # FINAL, or an unknown reject type: fail closed
    return PENDING, False, False, ()


class SumsubProvider:
    name = NAME

    def __init__(self, cfg: KycConfig, transport: Optional[Transport] = None,
                 clock: Callable[[], float] = time.time) -> None:
        missing = [k for k, v in (("KYC_APP_TOKEN", cfg.app_token), ("KYC_SECRET_KEY", cfg.secret_key),
                                  ("KYC_WEBHOOK_SECRET", cfg.webhook_secret), ("KYC_LEVEL_NAME", cfg.level_name))
                   if not v]
        if missing:
            raise KycNotConfigured("KYC provider not configured", missing=missing)
        if not cfg.api_base.startswith("https://"):
            raise KycNotConfigured("KYC api base must be https")
        self.cfg = cfg
        self._transport = transport or _urllib_transport
        self._clock = clock

    # ------------------------------------------------------------------------------------------------- REST
    def _request(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None,
                 body: Optional[Mapping[str, Any]] = None) -> tuple[int, dict]:
        path_q = path + ("?" + urllib.parse.urlencode(query) if query else "")
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else b""
        ts = str(int(self._clock()))
        headers = {
            "Accept": "application/json",
            "X-App-Token": self.cfg.app_token,
            "X-App-Access-Ts": ts,
            "X-App-Access-Sig": sign_request(self.cfg.secret_key, ts, method, path_q, raw),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, data = self._transport(method, self.cfg.api_base.rstrip("/") + path_q, headers,
                                       raw if body is not None else None)
        try:
            parsed = json.loads(data.decode("utf-8")) if data else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def _ok(self, status: int, data: dict, what: str) -> dict:
        if 200 <= status < 300:
            return data
        log.warning("kyc provider error", extra={"fields": {"what": what, "status": status,
                                                            "code": str(data.get("code", ""))[:40]}})
        raise ExternalServiceError(f"KYC provider {what} failed", status=status)

    def ensure_applicant(self, user_id: str) -> str:
        """Applicant id for our user (created at ``KYC_LEVEL_NAME`` on first use; 409 → look it up)."""
        status, data = self._request("POST", "/resources/applicants", {"levelName": self.cfg.level_name},
                                     {"externalUserId": user_id})
        if status == 409:
            status, data = self._request(
                "GET", f"/resources/applicants/-;externalUserId={urllib.parse.quote(user_id, safe='')}/one")
        data = self._ok(status, data, "applicant")
        applicant_id = str(data.get("id") or "")
        if not applicant_id or len(applicant_id) > 64 or not applicant_id.isalnum():
            raise ExternalServiceError("KYC provider returned no applicant id")
        ext = data.get("externalUserId")
        if ext is not None and ext != user_id:
            raise ExternalServiceError("KYC applicant belongs to another user")
        return applicant_id

    def websdk_link(self, user_id: str, return_url: str) -> str:
        body: dict[str, Any] = {"levelName": self.cfg.level_name, "userId": user_id, "ttlInSecs": self.cfg.link_ttl_s}
        if return_url:
            body["redirect"] = {"successUrl": return_url, "rejectUrl": return_url}   # [VERIFY] field names
        status, data = self._request("POST", "/resources/sdkIntegrations/levels/-/websdkLink", None, body)
        url = str(self._ok(status, data, "websdk link").get("url") or "")
        if not url.startswith("https://"):
            raise ExternalServiceError("KYC provider returned no https link")
        return url

    def access_token(self, user_id: str, ttl_s: int = 600) -> str:
        """Short-lived token for an embedded SDK (not used by the web app, which redirects to the hosted link)."""
        status, data = self._request("POST", "/resources/accessTokens/sdk", None,
                                     {"userId": user_id, "levelName": self.cfg.level_name, "ttlInSecs": ttl_s})
        token = str(self._ok(status, data, "access token").get("token") or "")
        if not token:
            raise ExternalServiceError("KYC provider returned no token")
        return token

    def fetch_applicant(self, applicant_id: str) -> dict:
        if not applicant_id.isalnum() or len(applicant_id) > 64:
            raise ValidationFailed("bad applicant id")
        status, data = self._request("GET", f"/resources/applicants/{applicant_id}/one")
        return self._ok(status, data, "applicant fetch")

    def create_session(self, *, user_id: str, return_url: str) -> KycSession:
        if not is_user_id(user_id):
            raise ValidationFailed("user_id must be a UUID")
        applicant_id = self.ensure_applicant(user_id)
        return KycSession(provider=NAME, provider_ref=applicant_id, status=PENDING,
                          url=self.websdk_link(user_id, return_url))

    # ---------------------------------------------------------------------------------------------- webhook
    def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> dict:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("pass the raw request body as bytes")
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        digest = h.get("x-payload-digest", "").strip().lower()
        alg = h.get("x-payload-digest-alg", "").strip().upper()
        if not digest or len(digest) > 256:
            raise KycWebhookError("missing payload digest")
        if alg not in DIGEST_ALGS:
            raise KycWebhookError("unsupported or missing payload digest algorithm")
        if not hmac.compare_digest(payload_digest(self.cfg.webhook_secret, bytes(payload), alg), digest):
            raise KycWebhookError("signature mismatch")
        try:
            body = json.loads(bytes(payload).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise KycWebhookError("payload is not JSON") from None
        if not isinstance(body, dict) or not body.get("type") or not body.get("applicantId"):
            raise KycWebhookError("payload is not a KYC notification")
        return body

    def parse_webhook(self, payload: bytes, headers: Mapping[str, str]) -> Optional[KycEvent]:
        """Verified, mapped event — or None for notifications we do not act on (sandbox in prod, not our user)."""
        body = self.verify_webhook(payload, headers)
        etype = str(body.get("type"))[:64]
        applicant_id = str(body.get("applicantId"))[:64]
        user_id = str(body.get("externalUserId") or "")
        if self.cfg.require_live and body.get("sandboxMode") is True:
            log.warning("kyc sandbox notification ignored in prod", extra={"fields": {"type": etype}})
            return None
        if not is_user_id(user_id):
            log.warning("kyc notification for an unknown external user", extra={"fields": {"type": etype}})
            return None
        review: Mapping[str, Any] = {"reviewStatus": body.get("reviewStatus"),
                                     "reviewResult": body.get("reviewResult") or {}}
        if self.cfg.refetch and etype not in ("applicantDeleted",):
            applicant = self.fetch_applicant(applicant_id)
            if str(applicant.get("id") or "") != applicant_id or applicant.get("externalUserId") != user_id:
                raise ExternalServiceError("KYC applicant re-fetch does not match the notification")
            fetched = applicant.get("review")
            review = fetched if isinstance(fetched, Mapping) else {}
        status, final, revoke, labels = map_review(etype, review)
        return KycEvent(provider=NAME, provider_ref=applicant_id, user_id=user_id, event_type=etype, status=status,
                        final=final, revoke=revoke, reject_labels=labels,
                        event_id=str(body.get("correlationId") or "")[:128])
