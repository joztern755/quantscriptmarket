"""POST /v1/webhooks/kyc through the real app with in-memory fakes and the Sumsub-style provider on a fake transport.
Skipped where FastAPI / httpx are not installed (they are in CI)."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
import urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.kyc.base import KycConfig  # noqa: E402
from app.kyc.sumsub import SumsubProvider, payload_digest  # noqa: E402

_HAVE_API = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "httpx", "pydantic"))
APPLICANT = "5f1a2b3c4d5e6f7a8b9c0d1e"
CFG = KycConfig(provider="sumsub", app_token="tst:app", secret_key="sk", webhook_secret="hook-secret",
                level_name="creator-kyc")


def transport_for(user_id, answer="GREEN", reject_type=None):
    rr = {"reviewAnswer": answer, **({"reviewRejectType": reject_type} if reject_type else {})}

    def t(method, url, headers, body):
        path = urllib.parse.urlsplit(url).path
        assert (method, path) == ("GET", f"/resources/applicants/{APPLICANT}/one"), (method, path)
        return 200, json.dumps({"id": APPLICANT, "externalUserId": user_id,
                                "review": {"reviewStatus": "completed", "reviewResult": rr}}).encode()
    return t


def signed(user_id, answer="GREEN", secret="hook-secret"):
    body = {"type": "applicantReviewed", "applicantId": APPLICANT, "externalUserId": user_id, "correlationId": "c1",
            "reviewStatus": "completed", "reviewResult": {"reviewAnswer": answer}}
    raw = json.dumps(body).encode()
    return raw, {"X-Payload-Digest": payload_digest(secret, raw), "X-Payload-Digest-Alg": "HMAC_SHA256_HEX"}


@unittest.skipUnless(_HAVE_API, "FastAPI/httpx not installed")
class KycWebhookHttpTests(unittest.TestCase):
    def build(self, answer="GREEN", reject_type=None, kyc_row=True):
        from types import SimpleNamespace

        from fastapi.testclient import TestClient

        from app.api.main import create_app
        from app.api.testing import FakeStore, FakeWorld, make_services

        class Store(FakeStore):   # KYC rows for this test (kept local: testing.py belongs to the API team)
            def get_kyc(self, conn, user_id):
                row = self.w.kyc.get(user_id)
                return dict(row) if row else None

            def set_kyc_status(self, conn, user_id, status):
                if user_id not in self.w.kyc:
                    return 0
                self.w.kyc[user_id]["status"] = status
                return 1

        world = FakeWorld()
        u = world.add_user("fb-creator")
        if kyc_row:
            world.kyc[u["id"]] = {"provider": "sumsub", "provider_ref": APPLICANT, "status": "pending"}
        prov = SumsubProvider(CFG, transport=transport_for(u["id"], answer, reject_type))
        svc = make_services(world, store=Store(world), kyc=SimpleNamespace(parse_webhook=prov.parse_webhook))
        return world, u, TestClient(create_app(svc), raise_server_exceptions=False)

    def test_green_is_provider_approved_awaiting_one_admin(self):
        """Owner: a provider GREEN is never an approval by itself — stored as provider_approved, ops asked for ONE
        admin's confirmation (POST /v1/admin/users/{id}/kyc)."""
        world, u, c = self.build()
        raw, h = signed(u["id"])
        for _ in range(2):                                   # redelivery: second is "unchanged"
            r = c.post("/v1/webhooks/kyc", content=raw, headers=h)
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(world.kyc[u["id"]]["status"], "provider_approved")
        results = [a["payload"]["result"] for a in world.audit if a["action"] == "kyc.webhook"]
        self.assertEqual(results, ["updated", "unchanged"])
        self.assertEqual([a["kind"] for a in world.alerts if a["user_id"] is None], ["kyc_awaiting_admin"])

    def test_api_red_final_rejects_even_if_payload_green(self):
        world, u, c = self.build(answer="RED", reject_type="FINAL")
        raw, h = signed(u["id"], answer="GREEN")
        self.assertEqual(c.post("/v1/webhooks/kyc", content=raw, headers=h).status_code, 200)
        self.assertEqual(world.kyc[u["id"]]["status"], "rejected")

    def test_forged_signature_400_nothing_changes(self):
        world, u, c = self.build()
        raw, h = signed(u["id"], secret="attacker")
        self.assertEqual(c.post("/v1/webhooks/kyc", content=raw, headers=h).status_code, 400)
        self.assertEqual(c.post("/v1/webhooks/kyc", content=raw).status_code, 422)   # no digest header
        self.assertEqual(world.kyc[u["id"]]["status"], "pending")

    def test_unknown_applicant_is_audited_not_applied(self):
        world, u, c = self.build(kyc_row=False)
        raw, h = signed(u["id"])
        self.assertEqual(c.post("/v1/webhooks/kyc", content=raw, headers=h).status_code, 200)
        self.assertNotIn(u["id"], world.kyc)
        self.assertEqual([a["payload"]["result"] for a in world.audit if a["action"] == "kyc.webhook"],
                         ["unknown_applicant"])


if __name__ == "__main__":
    unittest.main()
