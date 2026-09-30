"""app.kyc: provider selection, manual provider, Sumsub-style signed requests / applicant / hosted link, webhook
signature verification + verdict re-fetch, status mapping and transitions. All offline (fake transport)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import unittest
import urllib.parse
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import kyc  # noqa: E402
from app.errors import ExternalServiceError, ValidationFailed  # noqa: E402
from app.kyc.base import KycConfig, KycEvent, KycNotConfigured, KycWebhookError, next_status  # noqa: E402
from app.kyc.manual import ManualProvider  # noqa: E402
from app.kyc.sumsub import SumsubProvider, map_review, payload_digest, sign_request  # noqa: E402

USER = "8c1f6a1e-0000-4000-8000-000000000001"
APPLICANT = "5f1a2b3c4d5e6f7a8b9c0d1e"
NOW = 1_790_000_000
CFG = KycConfig(provider="sumsub", app_token="tst:apptoken", secret_key="sumsub-secret", webhook_secret="hook-secret",
                level_name="creator-kyc")


class FakeTransport:
    """Routes (method, path) -> (status, json). Records every call with its headers and body."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append({"method": method, "url": url, "path": parsed.path, "query": parsed.query,
                           "headers": dict(headers), "body": body})
        status, data = self.routes[(method, parsed.path)]
        return status, json.dumps(data).encode()


def provider(routes, cfg=CFG):
    t = FakeTransport(routes)
    return SumsubProvider(cfg, transport=t, clock=lambda: NOW), t


def applicant(review=None, ext=USER, aid=APPLICANT):
    d = {"id": aid, "externalUserId": ext}
    if review is not None:
        d["review"] = review
    return d


LINK = ("POST", "/resources/sdkIntegrations/levels/-/websdkLink")
CREATE = ("POST", "/resources/applicants")


class SigningTests(unittest.TestCase):
    def test_request_signature_vector(self):
        expected = hmac.new(b"k", b"123POST/resources/applicants?levelName=x{}", hashlib.sha256).hexdigest()
        self.assertEqual(sign_request("k", "123", "post", "/resources/applicants?levelName=x", b"{}"), expected)

    def test_every_call_is_signed_over_path_query_and_body(self):
        p, t = provider({CREATE: (201, applicant()), LINK: (200, {"url": "https://in.sumsub.com/websdk/p/abc"})})
        p.create_session(user_id=USER, return_url="https://aijalon.trade/#/creator/kyc")
        self.assertEqual(len(t.calls), 2)
        for c in t.calls:
            h = c["headers"]
            self.assertEqual(h["X-App-Token"], "tst:apptoken")
            self.assertEqual(h["X-App-Access-Ts"], str(NOW))
            path_q = c["path"] + ("?" + c["query"] if c["query"] else "")
            self.assertEqual(h["X-App-Access-Sig"],
                             sign_request("sumsub-secret", str(NOW), c["method"], path_q, c["body"] or b""))
            self.assertNotIn("sumsub-secret", json.dumps(h))
        self.assertEqual(t.calls[0]["query"], "levelName=creator-kyc")
        self.assertEqual(json.loads(t.calls[0]["body"]), {"externalUserId": USER})


class SessionTests(unittest.TestCase):
    def test_create_session_returns_hosted_link_and_applicant_ref(self):
        p, t = provider({CREATE: (201, applicant()), LINK: (200, {"url": "https://in.sumsub.com/websdk/p/abc"})})
        s = kyc.create_session(user_id=USER, return_url="https://aijalon.trade/#/creator/kyc", provider=p)
        self.assertEqual(s, {"provider": "sumsub", "provider_ref": APPLICANT, "status": "pending",
                             "url": "https://in.sumsub.com/websdk/p/abc"})
        link_body = json.loads(t.calls[1]["body"])
        self.assertEqual((link_body["levelName"], link_body["userId"]), ("creator-kyc", USER))
        self.assertEqual(link_body["redirect"]["successUrl"], "https://aijalon.trade/#/creator/kyc")

    def test_existing_applicant_is_reused(self):
        by_ext = ("GET", f"/resources/applicants/-;externalUserId={USER}/one")
        p, t = provider({CREATE: (409, {"code": 409, "description": "already exists"}), by_ext: (200, applicant()),
                         LINK: (200, {"url": "https://in.sumsub.com/websdk/p/x"})})
        self.assertEqual(p.create_session(user_id=USER, return_url="").provider_ref, APPLICANT)
        self.assertEqual([c["method"] for c in t.calls], ["POST", "GET", "POST"])

    def test_applicant_of_other_user_rejected(self):
        p, _ = provider({CREATE: (201, applicant(ext="someone-else"))})
        with self.assertRaises(ExternalServiceError):
            p.create_session(user_id=USER, return_url="")

    def test_provider_errors_are_retryable_5xx(self):
        p, _ = provider({CREATE: (500, {})})
        with self.assertRaises(ExternalServiceError):
            p.create_session(user_id=USER, return_url="")
        p, _ = provider({CREATE: (201, applicant()), LINK: (200, {"url": "http://insecure"})})
        with self.assertRaises(ExternalServiceError):
            p.create_session(user_id=USER, return_url="")

    def test_non_uuid_user_rejected(self):
        p, t = provider({})
        with self.assertRaises(ValidationFailed):
            p.create_session(user_id="../admin", return_url="")
        self.assertEqual(t.calls, [])

    def test_access_token(self):
        p, t = provider({("POST", "/resources/accessTokens/sdk"): (200, {"token": "_act-abc", "userId": USER})})
        self.assertEqual(p.access_token(USER), "_act-abc")
        self.assertEqual(json.loads(t.calls[0]["body"])["levelName"], "creator-kyc")

    def test_missing_config_fails_closed(self):
        with self.assertRaises(KycNotConfigured):
            SumsubProvider(KycConfig(provider="sumsub", app_token="x", secret_key="", webhook_secret="w",
                                     level_name="l"))
        with self.assertRaises(KycNotConfigured):
            SumsubProvider(KycConfig(provider="sumsub", app_token="x", secret_key="s", webhook_secret="w",
                                     level_name="l", api_base="http://api.sumsub.com"))


def hook(body, secret="hook-secret", alg="HMAC_SHA256_HEX"):
    raw = json.dumps(body).encode()
    return raw, {"X-Payload-Digest": payload_digest(secret, raw, alg), "X-Payload-Digest-Alg": alg}


def reviewed(answer="GREEN", reject_type=None, ext=USER, etype="applicantReviewed", sandbox=False):
    rr = {"reviewAnswer": answer}
    if reject_type:
        rr["reviewRejectType"] = reject_type
        rr["rejectLabels"] = ["FORGERY"]
    return {"type": etype, "applicantId": APPLICANT, "externalUserId": ext, "correlationId": "req-1",
            "levelName": "creator-kyc", "sandboxMode": sandbox, "reviewStatus": "completed", "reviewResult": rr}


GET_ONE = ("GET", f"/resources/applicants/{APPLICANT}/one")


def api_review(answer="GREEN", reject_type=None, status="completed"):
    rr = {"reviewAnswer": answer}
    if reject_type:
        rr["reviewRejectType"] = reject_type
    return applicant({"reviewStatus": status, "reviewResult": rr})


class WebhookTests(unittest.TestCase):
    def test_green_verified_and_refetched(self):
        p, t = provider({GET_ONE: (200, api_review("GREEN"))})
        ev = p.parse_webhook(*hook(reviewed("GREEN")))
        self.assertEqual((ev.status, ev.user_id, ev.provider_ref, ev.provider), ("approved", USER, APPLICANT, "sumsub"))
        self.assertEqual(t.calls[0]["path"], GET_ONE[1])

    def test_api_verdict_wins_over_payload(self):
        # a replayed/forged-with-leaked-secret GREEN is overridden by what the provider API says now
        p, _ = provider({GET_ONE: (200, api_review("RED", "FINAL"))})
        ev = p.parse_webhook(*hook(reviewed("GREEN")))
        self.assertEqual((ev.status, ev.final), ("rejected", True))

    def test_refetch_mismatch_is_error(self):
        p, _ = provider({GET_ONE: (200, applicant({"reviewStatus": "completed"}, ext="8c1f6a1e-0000-4000-8000-00000000000f"))})
        with self.assertRaises(ExternalServiceError):
            p.parse_webhook(*hook(reviewed()))

    def test_bad_signature(self):
        p, t = provider({})
        raw, h = hook(reviewed(), secret="attacker")
        with self.assertRaises(KycWebhookError):
            p.parse_webhook(raw, h)
        raw, h = hook(reviewed())
        with self.assertRaises(KycWebhookError):
            p.parse_webhook(raw.replace(b"GREEN", b"GREEM"), h)
        self.assertEqual(t.calls, [])

    def test_weak_or_missing_algorithm_refused(self):
        p, _ = provider({})
        raw = json.dumps(reviewed()).encode()
        sha1 = hmac.new(b"hook-secret", raw, hashlib.sha1).hexdigest()
        for h in ({"X-Payload-Digest": sha1, "X-Payload-Digest-Alg": "HMAC_SHA1_HEX"},
                  {"X-Payload-Digest": payload_digest("hook-secret", raw)}, {}):
            with self.assertRaises(KycWebhookError):
                p.parse_webhook(raw, h)

    def test_sha512_and_lowercase_headers(self):
        p, _ = provider({GET_ONE: (200, api_review())})
        raw, h = hook(reviewed(), alg="HMAC_SHA512_HEX")
        self.assertEqual(p.parse_webhook(raw, {k.lower(): v for k, v in h.items()}).status, "approved")

    def test_sandbox_ignored_in_prod(self):
        cfg = KycConfig(**{**CFG.__dict__, "require_live": True})
        p, t = provider({}, cfg)
        self.assertIsNone(p.parse_webhook(*hook(reviewed(sandbox=True))))
        self.assertEqual(t.calls, [])

    def test_unknown_external_user_ignored(self):
        p, _ = provider({})
        self.assertIsNone(p.parse_webhook(*hook(reviewed(ext="dashboard-created"))))

    def test_reset_revokes(self):
        p, _ = provider({GET_ONE: (200, applicant({"reviewStatus": "init"}))})
        ev = p.parse_webhook(*hook(reviewed(etype="applicantReset")))
        self.assertEqual((ev.status, ev.revoke), ("pending", True))

    def test_not_json_or_not_kyc(self):
        p, _ = provider({})
        raw = b"not json"
        with self.assertRaises(KycWebhookError):
            p.parse_webhook(raw, {"X-Payload-Digest": payload_digest("hook-secret", raw),
                                  "X-Payload-Digest-Alg": "HMAC_SHA256_HEX"})
        with self.assertRaises(KycWebhookError):
            p.parse_webhook(*hook({"foo": 1}))


class MappingTests(unittest.TestCase):
    def test_map_review(self):
        rv = lambda status, answer=None, rt=None: {"reviewStatus": status, "reviewResult": {  # noqa: E731
            **({"reviewAnswer": answer} if answer else {}), **({"reviewRejectType": rt} if rt else {})}}
        cases = [
            ("applicantReviewed", rv("completed", "GREEN"), ("approved", False, False)),
            ("applicantReviewed", rv("completed", "RED", "FINAL"), ("rejected", True, False)),
            ("applicantReviewed", rv("completed", "RED", "RETRY"), ("pending", False, False)),
            ("applicantReviewed", rv("completed", "RED"), ("rejected", True, False)),         # unknown -> closed
            ("applicantPending", rv("pending"), ("pending", False, False)),
            ("applicantOnHold", rv("onHold"), ("pending", False, False)),
            ("applicantCreated", rv("init"), ("pending", False, False)),
            ("applicantReviewed", rv("completed"), ("pending", False, False)),
            ("applicantDeleted", rv("completed", "GREEN"), ("pending", False, True)),
        ]
        for etype, review, want in cases:
            self.assertEqual(map_review(etype, review)[:3], want, (etype, review))

    def test_next_status(self):
        ev = lambda status, revoke=False: KycEvent("sumsub", APPLICANT, USER, "t", status, revoke=revoke)  # noqa: E731
        # owner: a provider GREEN is never an approval by itself — ONE admin confirms provider_approved → approved
        self.assertEqual(next_status("pending", ev("approved")), "provider_approved")
        self.assertEqual(next_status("rejected", ev("approved")), "provider_approved")
        self.assertIsNone(next_status("provider_approved", ev("approved")))       # redelivery
        self.assertIsNone(next_status("approved", ev("approved")))                # never downgrades an admin approval
        self.assertEqual(next_status("approved", ev("rejected")), "rejected")
        self.assertEqual(next_status("provider_approved", ev("rejected")), "rejected")
        self.assertIsNone(next_status("approved", ev("pending")))                 # routine pending never downgrades
        self.assertIsNone(next_status("provider_approved", ev("pending")))
        self.assertEqual(next_status("approved", ev("pending", revoke=True)), "pending")
        self.assertEqual(next_status("provider_approved", ev("pending", revoke=True)), "pending")
        self.assertEqual(next_status("rejected", ev("pending")), "pending")       # resubmission allowed
        self.assertIsNone(next_status("pending", ev("bogus")))


class SelectionAndManualTests(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("KYC_")}

    def tearDown(self):
        os.environ.update(self._env)

    def test_default_is_manual(self):
        s = kyc.create_session(user_id=USER, return_url="https://aijalon.trade/#/creator/kyc",
                               settings=SimpleNamespace(is_prod=True))
        self.assertEqual(s, {"provider": "manual", "provider_ref": f"manual:{USER}", "status": "pending", "url": "",
                             "manual": True})

    def test_manual_has_no_webhook(self):
        with self.assertRaises(KycWebhookError):
            ManualProvider().parse_webhook(b"{}", {})

    def test_manual_rejects_bad_user(self):
        with self.assertRaises(ValidationFailed):
            ManualProvider().create_session(user_id="x", return_url="")

    def test_env_fallback_and_settings_precedence(self):
        os.environ.update({"KYC_PROVIDER": "sumsub", "KYC_APP_TOKEN": "APPTOKENVALUE", "KYC_SECRET_KEY": "SECRETVALUE",
                           "KYC_WEBHOOK_SECRET": "HOOKVALUE", "KYC_LEVEL_NAME": "l"})
        self.assertIsInstance(kyc.get_provider(SimpleNamespace()), SumsubProvider)
        self.assertIsInstance(kyc.get_provider(SimpleNamespace(kyc_provider="manual")), ManualProvider)
        cfg = KycConfig.from_settings(SimpleNamespace(is_prod=True))
        self.assertTrue(cfg.require_live)
        for secret in ("APPTOKENVALUE", "SECRETVALUE", "HOOKVALUE"):
            self.assertNotIn(secret, repr(cfg))

    def test_sumsub_without_secrets_fails_closed(self):
        with self.assertRaises(KycNotConfigured):
            kyc.get_provider(SimpleNamespace(kyc_provider="sumsub"))

    def test_unknown_provider(self):
        with self.assertRaises(KycNotConfigured):
            kyc.get_provider(SimpleNamespace(kyc_provider="acme"))


if __name__ == "__main__":
    unittest.main()
