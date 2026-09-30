"""HTTP-level tests of the security-fix round (FastAPI TestClient + the in-memory fakes of app/api/testing.py).
Skipped when FastAPI / httpx / pytest are not installed (they run in CI). The SQL behind each fix is covered against
a real Postgres in tests/test_fix_api_money_db.py and tests/test_fix_api_account_db.py.

REVIEW_AUTH_API: F2 (step-up on plan / post purchase / Telegram re-link, post price cap), F3 (unpause), F5 (holds +
admin view), F6 (stale maker-checker), F12 (admin allowlist, suspending an admin), F13 (no admin override on creator
routes), F14 (review eligibility), F15 (same-day plan re-upgrade). REVIEW_MONEY: H5 (delisting), M4 (typed data once,
no reject after issue).
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

from test_api_http import (  # noqa: E402
    USD,
    W1,
    W2,
    _admins,
    _subscribe_world,
    _user_with_wallet,
    build,
    key,
    login,
)

from app.api.testing import make_settings  # noqa: E402


def _err(r):
    return r.json()["error"]


# ================================================================================================= F2
def test_plan_change_needs_step_up_and_same_day_reupgrade_is_charged():
    world, svc, c = build()
    u = _user_with_wallet(world, balance=200 * USD)
    stale = login(svc, world, stale=True)
    r = c.post("/v1/me/plan", headers={**stale, "Idempotency-Key": key()}, json={"plan": "max"})
    assert r.status_code == 401 and _err(r)["code"] == "step_up_required"         # a stolen session cannot spend
    h = login(svc, world)
    r = c.post("/v1/me/plan", headers={**h, "Idempotency-Key": key()}, json={"plan": "max"})
    assert r.status_code == 200 and r.json()["charged_micro"] == 50 * USD
    assert c.post("/v1/me/plan", headers={**h, "Idempotency-Key": key()}, json={"plan": "free"}).status_code == 200
    r = c.post("/v1/me/plan", headers={**h, "Idempotency-Key": key()}, json={"plan": "max"})
    assert r.status_code == 200 and r.json()["charged_micro"] == 50 * USD
    plan_txs = [k for k, tx in world.ledger_tx.items() if tx["kind"] == "plan_purchase"]
    assert len(plan_txs) == 2                                                        # F15: charged twice, not once
    assert world.fee_balance(u["id"]) == 100 * USD


def test_post_purchase_needs_step_up_and_respects_the_price_cap():
    world, svc, c = build()
    u = _user_with_wallet(world, balance=900 * USD)
    world.users[u["id"]]["plan"] = "pro"
    creator = world.add_user("fb-creator", role="creator")
    ok = svc.store.add_post(creator_id=creator["id"], price_micro=10 * USD)
    too_dear = svc.store.add_post(creator_id=creator["id"], price_micro=800 * USD)
    stale = login(svc, world, stale=True)
    r = c.post(f"/v1/posts/{ok['id']}/purchase", headers={**stale, "Idempotency-Key": key()})
    assert r.status_code == 401 and _err(r)["code"] == "step_up_required"
    h = login(svc, world)
    r = c.post(f"/v1/posts/{ok['id']}/purchase", headers={**h, "Idempotency-Key": key()})
    assert r.status_code == 201, r.text
    r = c.post(f"/v1/posts/{too_dear['id']}/purchase", headers={**h, "Idempotency-Key": key()})
    assert r.status_code == 409 and _err(r)["details"]["reason"] == "post_price_above_cap"


def test_telegram_relink_needs_step_up_and_warns_the_current_chat(monkeypatch):
    from types import SimpleNamespace

    from app.api.routers import alerts_settings as A
    world, svc, c = build()
    u = world.add_user("fb-user")
    state = {"linked": False}
    monkeypatch.setattr(A.user_sinks, "contact_status",
                        lambda conn, uid, now: SimpleNamespace(telegram="linked" if state["linked"] else "unlinked"))
    monkeypatch.setattr(A.telegram_bot, "create_link",
                        lambda conn, **kw: {"url": "https://t.me/bot?start=x", "expires_at": world.now})
    stale = login(svc, world, stale=True)
    assert c.post("/v1/alerts/telegram/link", headers=stale).status_code == 200     # first link: no step-up
    state["linked"] = True
    r = c.post("/v1/alerts/telegram/link", headers=stale)
    assert r.status_code == 401 and _err(r)["code"] == "step_up_required"
    assert c.post("/v1/alerts/telegram/link", headers=login(svc, world)).status_code == 200
    mine = [a for a in world.alerts if a["user_id"] == u["id"] and a["kind"] == "alert_contacts_changed"]
    assert len(mine) == 1 and mine[0]["severity"] == "critical"


# ================================================================================================= F5
def test_withdrawal_refused_to_a_fresh_wallet_and_during_a_security_hold():
    world, svc, c = build()
    u = world.add_user("fb-user")
    world.add_wallet(u["id"], W1, verified_at=world.now - timedelta(hours=1))
    world.credit(u["id"], 100 * USD)
    h = login(svc, world)
    r = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": key()}, json={"amount": "20", "to_address": W1})
    assert r.status_code == 403 and _err(r)["details"]["reason"] == "payout_address_hold"
    world.add_wallet(u["id"], W2)                                            # verified a week ago
    svc.store.extend_security_hold(None, u["id"], world.now + timedelta(hours=48))   # e.g. MFA just changed
    r = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": key()}, json={"amount": "20", "to_address": W2})
    assert r.status_code == 403 and _err(r)["details"]["reason"] == "security_hold"
    world.users[u["id"]]["security_hold_until"] = world.now - timedelta(minutes=1)
    r = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": key()}, json={"amount": "20", "to_address": W2})
    assert r.status_code == 201, r.text


def test_second_approval_blocked_during_hold_and_admin_sees_context():
    world, svc, c = build()
    u = _user_with_wallet(world, balance=100 * USD)
    _admins(world)
    r = c.post("/v1/withdrawals", headers={**login(svc, world), "Idempotency-Key": key()},
               json={"amount": "30", "to_address": W1})
    wid = r.json()["id"]
    h1, h2 = login(svc, world, "fb-admin1"), login(svc, world, "fb-admin2")
    r = c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h1)
    assert r.status_code == 200 and r.json()["wallet_age_hours"] >= 48 and r.json()["hold_reasons"] == []
    svc.store.extend_security_hold(None, u["id"], world.now + timedelta(hours=48))
    r = c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h2)
    assert r.status_code == 409 and _err(r)["details"]["reason"] == "security_hold"


# ================================================================================================= M4
def test_typed_data_issued_once_and_reject_blocked_after_issue():
    world, svc, c = build()
    _user_with_wallet(world, balance=100 * USD)
    _admins(world)
    wid = c.post("/v1/withdrawals", headers={**login(svc, world), "Idempotency-Key": key()},
                 json={"amount": "30", "to_address": W1}).json()["id"]
    h1, h2 = login(svc, world, "fb-admin1"), login(svc, world, "fb-admin2")
    c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h1)
    c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h2)
    r1 = c.post(f"/v1/admin/payouts/withdrawal/{wid}/typed-data", headers=h1, json={"signature_chain_id": "0xa4b1"})
    world.now = world.now + timedelta(minutes=10)
    r2 = c.post(f"/v1/admin/payouts/withdrawal/{wid}/typed-data", headers=login(svc, world, "fb-admin2"),
                json={"signature_chain_id": "0xa4b1"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["payload"] == r2.json()["payload"]                     # same nonce → one possible transfer
    r = c.post(f"/v1/admin/payouts/withdrawal/{wid}/reject", headers=login(svc, world, "fb-admin1"),
               json={"reason": "second thoughts"})
    assert r.status_code == 409 and _err(r)["details"]["reason"] == "send_in_progress"


# ================================================================================================= F3 / H3
def test_unpause_after_the_period_ended_requires_the_renewal():
    world, svc, c = build()
    u, st, creator = _subscribe_world(world, svc, balance=5 * USD)
    sub = svc.store.insert_subscription(None, user_id=u["id"], strategy_id=st["id"],
                                        version_id=next(iter(world.versions)), trading_address=W1, master_address=W1,
                                        allocation_micro=100 * USD, max_leverage_x100=100, status="reduce_only",
                                        current_period_end=world.now - timedelta(days=3), price_monthly_micro=20 * USD,
                                        profit_share_bps=1000)
    world.subscriptions[sub["id"]]["past_due_since"] = world.now - timedelta(days=3)
    h = login(svc, world)
    assert c.patch(f"/v1/subscriptions/{sub['id']}", headers=h, json={"paused": True}).status_code == 200
    assert world.subscriptions[sub["id"]]["pre_pause_status"] == "reduce_only"
    r = c.patch(f"/v1/subscriptions/{sub['id']}", headers=h, json={"paused": False})
    assert r.status_code == 402 and _err(r)["details"]["reason"] == "renewal_due"       # was: straight to active
    assert world.subscriptions[sub["id"]]["status"] == "paused_user"
    world.credit(u["id"], 30 * USD)
    r = c.patch(f"/v1/subscriptions/{sub['id']}", headers=h, json={"paused": False})
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert sum(1 for k in world.ledger_tx if k.startswith(f"sub:{sub['id']}:")) == 1


# ================================================================================================= H5 / F6
def test_delisting_ends_subscriptions_and_cancels_pending_listing():
    world, svc, c = build()
    u, st, creator = _subscribe_world(world, svc)
    a1, a2 = _admins(world)
    sub = svc.store.insert_subscription(None, user_id=u["id"], strategy_id=st["id"],
                                        version_id=next(iter(world.versions)), trading_address=W1, master_address=W1,
                                        allocation_micro=100 * USD, max_leverage_x100=100, status="active",
                                        current_period_end=world.now + timedelta(days=10))
    ch = svc.store.insert_change(None, kind="strategy_list", target=f"strategy:{st['id']}",
                                 payload={"version_id": next(iter(world.versions)), "version": 1}, reason="list v1 now",
                                 maker=a1["id"])
    h2 = login(svc, world, "fb-admin2")
    r = c.post(f"/v1/admin/strategies/{st['id']}/delist", headers=h2, json={"reason": "malicious behaviour"})
    assert r.status_code == 200, r.text
    s = world.subscriptions[sub["id"]]
    assert (s["status"], s["cancel_positions"], s["end_reason"]) == ("closing", "close", "strategy_delisted")
    assert any(a["kind"] == "strategy_ended" and a["user_id"] == u["id"] for a in world.alerts)
    assert world.changes[ch["id"]]["status"] == "cancelled"
    r = c.post(f"/v1/admin/changes/{ch['id']}/approve", headers=h2, json={"reason": "approve stale one"})
    assert r.status_code == 404                                              # no longer pending → never relisted


def test_listing_approval_refuses_terms_changed_since_the_proposal():
    world, svc, c = build()
    creator = world.add_user("fb-creator", role="creator")
    st, ver = world.add_strategy("alpha", price=20 * USD, profit_share_bps=500, in_house=False, owner=creator["id"],
                                 status="review")
    a1, a2 = _admins(world)
    world.kyc[creator["id"]] = {"provider": "manual", "provider_ref": "m", "status": "approved"}
    ch = svc.store.insert_change(None, kind="strategy_list", target=f"strategy:{st['id']}",
                                 payload={"version_id": ver["id"], "version": 1,
                                          "terms": {"price_monthly_micro": 20 * USD, "profit_share_bps": 500,
                                                    "owner_user_id": creator["id"]}},
                                 reason="reviewed terms", maker=a1["id"])
    world.strategies[st["id"]]["profit_share_bps"] = 1200                     # raised after the review
    r = c.post(f"/v1/admin/changes/{ch['id']}/approve", headers=login(svc, world, "fb-admin2"),
               json={"reason": "approve listing"})
    assert r.status_code == 409 and _err(r)["details"]["reason"] == "terms_changed"
    assert world.strategies[st["id"]]["status"] == "review"


# ================================================================================================= F12 / F13
def test_admin_needs_the_email_allowlist_and_cannot_suspend_another_admin_alone():
    s = make_settings(launch_phase="public", payouts_enabled=True, admin_emails=("a1@aijalon.trade",))
    world, svc, c = build(s)
    a1, a2 = _admins(world)
    h2 = login(svc, world, "fb-admin2", email="a2@aijalon.trade")
    r = c.get("/v1/admin/flags", headers=h2)
    assert r.status_code == 403 and _err(r)["details"]["reason"] == "admin_not_allowlisted"
    h1 = login(svc, world, "fb-admin1", email="a1@aijalon.trade")
    r = c.post(f"/v1/admin/users/{a2['id']}/suspend", headers=h1, json={"reason": "lost laptop"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert world.users[a2["id"]]["status"] == "active"


def test_admin_has_no_override_on_creator_routes():
    world, svc, c = build()
    creator = world.add_user("fb-creator", role="creator")
    st, _ = world.add_strategy("alpha", price=20 * USD, in_house=False, owner=creator["id"], status="draft")
    a1, _ = _admins(world)
    from app.api.deps import DEFAULT_LEGAL_VERSIONS
    world.consents.append({"user_id": a1["id"], "doc": "creator_agreement",
                           "doc_version": DEFAULT_LEGAL_VERSIONS["creator_agreement"], "doc_text_sha256": "0" * 64,
                           "context": "creator", "strategy_id": None, "accepted_at": world.now})
    r = c.patch(f"/v1/creator/strategies/{st['id']}", headers=login(svc, world, "fb-admin1"),
                json={"price_monthly_micro": 1})
    assert r.status_code == 404
    assert world.strategies[st["id"]]["price_monthly_micro"] == 20 * USD
