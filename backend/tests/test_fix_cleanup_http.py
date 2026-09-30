"""HTTP-level tests of the final clean-up round (FastAPI TestClient + the in-memory fakes of app/api/testing.py).
Skipped when FastAPI / httpx / pytest are not installed (they run in CI). The SQL behind each change is covered
against a real Postgres in tests/test_fix_cleanup_db.py; the pure logic in tests/test_fix_cleanup_units.py.

- referrer payouts need KYC approved (same per-user record and one-admin decision as creators); GET /referrals shows
  the status; the admin approval re-checks it;
- admin strategy pause: mandatory strategy_paused alert to every live subscriber, no new subscription while paused,
  unpause (listing approval) credits the paused time back to the running period.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from test_api_http import USD, W1, _admins, _user_with_wallet, build, key, login  # noqa: E402


def _err(r):
    return r.json()["error"]


def test_referrer_payout_needs_kyc_and_referrals_shows_the_status():
    world, svc, c = build()
    u = _user_with_wallet(world)
    world.balances[f"referrer:{u['id']}:payable"] = -50 * USD
    h = login(svc, world)
    r = c.get("/v1/referrals", headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["kyc_status"], r.json()["payout_kyc_required"]) == ("none", True)
    r = c.post("/v1/payouts", headers={**h, "Idempotency-Key": key()},
               json={"amount": "20", "source": "referrer", "to_address": W1})
    assert r.status_code == 403 and _err(r)["details"]["reason"] == "kyc_required"
    world.kyc[u["id"]] = {"provider": "manual", "provider_ref": "m", "status": "pending"}
    assert c.get("/v1/referrals", headers=h).json()["kyc_status"] == "pending"
    r = c.post("/v1/payouts", headers={**h, "Idempotency-Key": key()},
               json={"amount": "20", "source": "referrer", "to_address": W1})
    assert r.status_code == 403 and _err(r)["details"]["kyc_status"] == "pending"
    world.kyc[u["id"]]["status"] = "approved"
    assert c.get("/v1/referrals", headers=h).json()["kyc_status"] == "approved"
    r = c.post("/v1/payouts", headers={**h, "Idempotency-Key": key()},
               json={"amount": "20", "source": "referrer", "to_address": W1})
    assert not (r.status_code == 403 and _err(r)["details"].get("reason") == "kyc_required"), r.text


def test_admin_pause_alerts_blocks_subscribe_and_unpause_credits_the_period():
    world, svc, c = build()
    st, ver = world.add_strategy("silverx", price=20 * USD, profit_share_bps=0, in_house=True, markets=("BTC",))
    u = _user_with_wallet(world, balance=100 * USD)
    end = world.now + timedelta(days=10)
    sub = svc.store.insert_subscription(None, user_id=u["id"], strategy_id=st["id"], version_id=ver["id"],
                                        trading_address=W1, master_address=W1, allocation_micro=100 * USD,
                                        max_leverage_x100=100, status="active", current_period_end=end)
    a1, a2 = _admins(world)
    r = c.post(f"/v1/admin/strategies/{st['id']}/pause", headers=login(svc, world, "fb-admin1"),
               json={"reason": "oracle incident review"})
    assert r.status_code == 200, r.text
    assert world.strategies[st["id"]]["status"] == "paused"
    mine = [a for a in world.alerts if a["user_id"] == u["id"] and a["kind"] == "strategy_paused"]
    assert len(mine) == 1 and mine[0]["severity"] == "warn"
    # five days later a second admin approves the re-listing proposed by the first
    world.now = world.now + timedelta(days=5)
    ch = svc.store.insert_change(None, kind="strategy_list", target=f"strategy:{st['id']}",
                                 payload={"version_id": ver["id"], "version": 1,
                                          "terms": {"price_monthly_micro": 20 * USD, "profit_share_bps": 0,
                                                    "owner_user_id": None}},
                                 reason="incident closed", maker=a1["id"])
    r = c.post(f"/v1/admin/changes/{ch['id']}/approve", headers=login(svc, world, "fb-admin2"),
               json={"reason": "resume"})
    assert r.status_code == 200, r.text
    assert world.strategies[st["id"]]["status"] == "listed"
    assert world.subscriptions[sub["id"]]["current_period_end"] == end + timedelta(days=5)
    assert any(a["kind"] == "strategy_resumed" and a["user_id"] == u["id"] for a in world.alerts)
