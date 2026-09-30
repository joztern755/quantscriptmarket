"""HTTP-level checks of the user-alert routes with the in-memory API fakes (app/api/testing.py).

Skipped when FastAPI / httpx / pytest are not installed (the DB-backed behaviour is covered by
tests/test_alerts_user_db.py against a real Postgres). Covers: the mandatory-contacts gate on POST /v1/subscriptions
(409 contacts_required), the Telegram webhook secret check (constant-time, 401 on mismatch, unconfigured → reject),
malformed updates acknowledged without touching the DB, mandatory kinds refused by PATCH /v1/alerts/prefs, and
the executor-only mounting of /v1/internal/deliver-alerts.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

HAVE = all(importlib.util.find_spec(m) is not None for m in ("fastapi", "httpx", "pytest"))
SECRET = "s" * 64


@unittest.skipUnless(HAVE, "needs fastapi + httpx + pytest")
class UserAlertsHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        import test_api_http as T
        from app.api.main import create_app
        from app.api.testing import FakeWorld, make_services, make_settings

        self.T = T
        self.world = FakeWorld()
        settings = make_settings(launch_phase="public", payouts_enabled=True, telegram_webhook_secret=SECRET)
        self.svc = make_services(self.world, settings=settings)
        self.c = TestClient(create_app(self.svc), raise_server_exceptions=False)

    def test_subscribe_requires_alert_contacts(self) -> None:
        T, world, svc, c = self.T, self.world, self.svc, self.c
        u, st, _ = T._subscribe_world(world, svc)
        world.alert_contacts_missing.add(str(u["id"]))
        h = T.login(svc, world)
        body = {"strategy_id": st["id"], "trading_address": T.W1, "allocation": "500", "max_leverage_x100": 200}
        r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": T.key()}, json=body)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["error"]["code"], "contacts_required")
        self.assertEqual(sorted(r.json()["error"]["details"]["missing"]), ["email", "telegram"])
        self.assertFalse(world.subscriptions)
        # once contacts are set up the request proceeds to the next check (the subscription acknowledgement)
        world.alert_contacts_missing.clear()
        r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": T.key()}, json=body)
        self.assertEqual(r.json()["error"]["details"]["reason"], "subscription_ack_required")

    def test_telegram_webhook_secret(self) -> None:
        url = "/v1/webhooks/telegram"
        upd = {"update_id": 1, "message": "not-a-dict"}
        self.assertEqual(self.c.post(url, json=upd).status_code, 401)
        self.assertEqual(self.c.post(url, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": "x" * 64}).status_code, 401)
        r = self.c.post(url, json=upd, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        r = self.c.post(url, content=b"not json", headers={"X-Telegram-Bot-Api-Secret-Token": SECRET,
                                                           "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)

    def test_prefs_refuse_mandatory(self) -> None:
        T = self.T
        self.world.add_user("fb-user")
        h = T.login(self.svc, self.world)
        r = self.c.patch("/v1/alerts/prefs", headers=h, json={"muted": {"agent_expired": True}})
        self.assertEqual(r.status_code, 422)
        r = self.c.patch("/v1/alerts/prefs", headers=h, json={"muted": {"trade_opened": "yes"}})
        self.assertEqual(r.status_code, 422)

    def test_internal_jobs_only_on_executor(self) -> None:
        self.assertEqual(self.c.post("/v1/internal/deliver-alerts").status_code, 404)
        from fastapi.testclient import TestClient

        from app.api.main import create_executor_app
        ex = TestClient(create_executor_app(self.svc), raise_server_exceptions=False)
        self.assertIn(ex.post("/v1/internal/deliver-alerts").status_code, (401, 403))   # OIDC required
        self.assertIn(ex.post("/v1/internal/daily-pnl-summary").status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
