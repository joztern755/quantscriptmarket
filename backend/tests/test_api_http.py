"""HTTP-level tests of the API (FastAPI TestClient) with in-memory fakes (app/api/testing.py).

Skipped when FastAPI / httpx are not installed. Covers: security headers, request id, body limit, CORS, error
envelope (no stack traces), Cloudflare edge trust + geo-block, OpenAPI off in prod, internal OIDC (executor
only), auth/MFA/step-up/consent gates, launch allowlist, referral binding, idempotency (replay, mismatch,
rollback), withdrawals, subscriptions (checks, charges, SPEC §12 cancel), Stripe webhook, admin maker-checker.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.api.deps import DEFAULT_LEGAL_VERSIONS, SITE_DOCS  # noqa: E402
from app.api.main import create_app, create_executor_app  # noqa: E402
from app.api.testing import FakeWorld, make_services, make_settings  # noqa: E402

WEB = "https://aijalon.trade"
EDGE = "e" * 40
W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
USD = 1_000_000


def key() -> str:
    return str(uuid.uuid4())


def build(settings=None, **overrides):
    world = FakeWorld()
    svc = make_services(world, settings=settings, **overrides)
    client = TestClient(create_app(svc), raise_server_exceptions=False)
    return world, svc, client


def login(svc, world, uid="fb-user", *, stale=False, mfa=True, email="u@example.com", **extra) -> dict:
    tok = f"tok-{uid}-{uuid.uuid4().hex[:6]}"
    at = world.now - timedelta(hours=2) if stale else world.now
    svc.auth.add(tok, uid, email=email, mfa=mfa, auth_time=at, **extra)
    return {"Authorization": f"Bearer {tok}"}


def site_consents(country=None) -> dict:
    items = []
    for d in SITE_DOCS:
        item = {"doc": d, "doc_version": DEFAULT_LEGAL_VERSIONS[d], "context": "site_entry", "doc_text_sha256": "a" * 64,
                "accepted_at": "2026-09-30T11:59:00Z"}
        if d == "jurisdiction" and country:
            item["country"] = country
        items.append(item)
    return {"consents": items}


# ================================================================================================= platform
def test_healthz_security_headers_and_request_id():
    _, _, c = build()
    r = c.get("/healthz", headers={"X-Request-ID": "req-12345678"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["x-request-id"] == "req-12345678"
    assert "max-age" in r.headers["strict-transport-security"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    r2 = c.get("/healthz", headers={"X-Request-ID": "bad id <script>"})
    assert r2.headers["x-request-id"] != "bad id <script>" and len(r2.headers["x-request-id"]) == 32


def test_unknown_route_json_envelope():
    _, _, c = build()
    r = c.get("/v1/nope")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "not_found" and body["request_id"]


def test_public_config_shape_and_cache():
    _, svc, c = build()
    r = c.get("/v1/public/config")
    assert r.status_code == 200
    cfg = r.json()
    assert cfg["economics"]["profit_share_creator_cap_bps"] == svc.settings.economics.profit_share_creator_cap_bps
    assert cfg["hl_chain"] in ("Mainnet", "Testnet") and cfg["builder_address"] == "0x" + "b" * 40
    assert set(cfg["legal_versions"]) >= set(SITE_DOCS)
    assert "public" in r.headers["cache-control"]


def test_body_limit_413():
    world, svc, c = build()
    world.add_user("fb-user")
    r = c.post("/v1/consents", headers=login(svc, world), content=b"{" + b" " * (200 * 1024) + b"}",
               )
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


def test_cors_only_web_origin_and_idempotency_header():
    _, _, c = build()
    ok = c.options("/v1/withdrawals", headers={"Origin": WEB, "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "authorization,content-type,idempotency-key"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == WEB
    assert "idempotency-key" in ok.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in ok.headers
    bad = c.options("/v1/withdrawals", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


def test_unhandled_error_is_generic_500():
    world, svc, c = build()

    def boom(*a, **k):
        raise RuntimeError("password=hunter2 stack detail")
    svc.store.get_user_by_firebase_uid = boom
    r = c.get("/v1/me", headers=login(svc, world))
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in r.text and "Traceback" not in r.text


def test_edge_trust_and_geo_block():
    world, svc, c = build(make_settings(edge_auth_secret=EDGE, launch_phase="public", payouts_enabled=True))
    # CF-IPCountry is ignored unless X-Edge-Auth proves the Cloudflare hop
    assert c.get("/v1/public/config", headers={"CF-IPCountry": "US"}).status_code == 200
    r = c.get("/v1/public/config", headers={"CF-IPCountry": "US", "X-Edge-Auth": EDGE})
    assert r.status_code == 451 and r.json()["error"]["code"] == "jurisdiction_restricted"
    assert c.get("/v1/public/config", headers={"CF-IPCountry": "T1", "X-Edge-Auth": EDGE}).status_code == 451
    assert c.get("/v1/public/config", headers={"CF-IPCountry": "MY", "X-Edge-Auth": EDGE}).status_code == 200
    # Stripe (US servers) is exempt: signature is the auth
    r = c.post("/v1/webhooks/stripe", headers={"CF-IPCountry": "US", "X-Edge-Auth": EDGE, "Stripe-Signature": "bad"},
               content=b"{}")
    assert r.status_code == 400


def _prod_settings(**kw):
    base = dict(env="prod", edge_auth_secret=EDGE, audit_pepper_b64="cGVwcGVy" * 8, sandbox_url="https://sandbox",
                scheduler_sa_email="scheduler@p.iam.gserviceaccount.com", internal_audience="https://exec.run.app",
                launch_phase="public", payouts_enabled=True, kms_key_name="k", firebase_project_id="p",
                signals_pubkey_b64="x")
    base.update(kw)
    return make_settings(**base)


def test_prod_requires_edge_and_hides_openapi():
    world, svc, c = build(_prod_settings())
    assert c.get("/openapi.json").status_code == 404
    r = c.get("/v1/public/config")
    assert r.status_code == 403 and r.json()["error"]["code"] == "edge_required"
    assert c.get("/v1/public/config", headers={"X-Edge-Auth": EDGE}).status_code == 200
    assert c.get("/healthz").status_code == 200


def test_prod_refuses_to_start_without_security_config():
    world = FakeWorld()
    with pytest.raises(RuntimeError):
        create_app(make_services(world, settings=_prod_settings(edge_auth_secret="")))


def test_dev_has_openapi_and_no_internal_routes_on_api():
    _, _, c = build()
    assert c.get("/openapi.json").status_code == 200
    assert c.post("/v1/internal/tick").status_code == 404


def test_executor_internal_requires_scheduler_oidc():
    world = FakeWorld()
    s = make_settings(scheduler_sa_email="scheduler@p.iam.gserviceaccount.com", internal_audience="https://exec",
                      launch_phase="public")
    svc = make_services(world, settings=s)
    c = TestClient(create_executor_app(svc), raise_server_exceptions=False)
    assert c.post("/v1/internal/tick").status_code == 401
    svc.oidc.tokens["good"] = {"aud": "https://exec", "email": "scheduler@p.iam.gserviceaccount.com",
                               "email_verified": True}
    svc.oidc.tokens["other"] = {"aud": "https://exec", "email": "someone@p.iam.gserviceaccount.com",
                                "email_verified": True}
    assert c.post("/v1/internal/tick", headers={"Authorization": "Bearer other"}).status_code == 403
    r = c.post("/v1/internal/settle-daily", headers={"Authorization": "Bearer good"}, json={"settle_date": "2026-09-29"})
    assert r.status_code == 200 and r.json()["job"] == "settle-daily"
    assert world.jobs_run[-1][0] == "settle-daily"
    assert any(a["action"] == "job.settle-daily" for a in world.audit)
    assert c.get("/v1/public/config").status_code == 404        # executor serves no public API


# ================================================================================================= auth & consent
def test_auth_required_and_mfa_enforced():
    world, svc, c = build()
    r = c.get("/v1/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = c.get("/v1/me", headers=login(svc, world, mfa=False))
    assert r.status_code == 401 and r.json()["error"]["code"] == "mfa_required"


def test_first_login_creates_user_with_referral_and_consent_gate():
    world, svc, c = build()
    ref = world.add_user("fb-ref", referral_code="REFCDE23")
    h = login(svc, world, "fb-new")
    r = c.get("/v1/me", headers={**h, "X-Ref-Code": "refcde23"})
    assert r.status_code == 200
    me = r.json()
    assert me["consents_complete"] is False and me["role"] == "user"
    new = next(u for u in world.users.values() if u["firebase_uid"] == "fb-new")
    assert new["referred_by"] == ref["id"]
    assert any(a["action"] == "user.created" for a in world.audit)
    r = c.get("/v1/balance", headers=h)
    assert r.status_code == 403 and r.json()["error"]["code"] == "consent_required"
    assert set(r.json()["error"]["details"]["missing"]) == set(SITE_DOCS)
    r = c.post("/v1/consents", headers=h, json=site_consents(country="MY"))
    assert r.status_code == 200 and r.json()["complete"] is True
    assert c.get("/v1/balance", headers=h).status_code == 200
    assert world.users[new["id"]]["country_attested"] == "MY"


def test_consent_version_hash_and_jurisdiction_rules():
    world, svc, c = build()
    world.add_user("fb-user", consents=False)
    h = login(svc, world)
    stale = site_consents()
    stale["consents"][0]["doc_version"] = "2020-01-01"
    assert c.post("/v1/consents", headers=h, json=stale).status_code == 409
    no_hash = site_consents()
    del no_hash["consents"][0]["doc_text_sha256"]
    r = c.post("/v1/consents", headers=h, json=no_hash)
    assert r.status_code == 422
    r = c.post("/v1/consents", headers=h, json=site_consents(country="US"))
    assert r.status_code == 451
    bad_ctx = {"consents": [{"doc": "subscription_ack", "doc_version": "2026-09-30", "context": "site_entry",
                             "doc_text_sha256": "a" * 64}]}
    assert c.post("/v1/consents", headers=h, json=bad_ctx).status_code == 422


def test_suspended_user_and_admin_role():
    world, svc, c = build()
    world.add_user("fb-s", status="suspended")
    assert c.get("/v1/me", headers=login(svc, world, "fb-s")).status_code == 403
    world.add_user("fb-user")
    assert c.get("/v1/admin/flags", headers=login(svc, world)).status_code == 403


def test_step_up_required_for_agents():
    world, svc, c = build()
    u = world.add_user("fb-user")
    world.add_wallet(u["id"], W1)
    r = c.post("/v1/agents", headers=login(svc, world, stale=True),
               json={"master_address": W1, "signature_chain_id": "0xa4b1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "step_up_required"


def test_internal_launch_allowlist():
    s = make_settings(launch_phase="internal", allowlist_emails=("ok@aijalon.trade",))
    world, svc, c = build(s)
    assert c.get("/v1/me", headers=login(svc, world, "fb-a", email="nope@x.io")).status_code == 403
    assert c.get("/v1/me", headers=login(svc, world, "fb-b", email="ok@aijalon.trade")).status_code == 200


def test_referral_bind_once_and_self_referral_blocked():
    world, svc, c = build()
    ref = world.add_user("fb-ref", referral_code="REFCDE23")
    u = world.add_user("fb-user")
    h = login(svc, world)
    r = c.patch("/v1/me", headers=h, json={"referral_code_used": "refcde23"})
    assert r.status_code == 200 and world.users[u["id"]]["referred_by"] == ref["id"]
    assert c.patch("/v1/me", headers=h, json={"referral_code_used": "refcde23"}).status_code == 409
    # same wallet on both sides → self-referral
    v = world.add_user("fb-v")
    world.add_wallet(ref["id"], W2)
    world.wallets[W2 + "x"] = {"user_id": v["id"], "address": W2, "verified_at": world.now, "created_at": world.now}
    r = c.patch("/v1/me", headers=login(svc, world, "fb-v"), json={"referral_code_used": "REFCDE23"})
    assert r.status_code == 403


# ================================================================================================= money
def _user_with_wallet(world, uid="fb-user", *, balance=0, withdrawable=True):
    u = world.add_user(uid)
    world.add_wallet(u["id"], W1)
    if balance:
        world.credit(u["id"], balance, withdrawable=withdrawable)
    return u


def test_withdrawal_idempotency_replay_mismatch_and_rollback():
    world, svc, c = build()
    u = _user_with_wallet(world, balance=100 * USD)
    h = login(svc, world)
    body = {"amount": "40", "to_address": W1}
    assert c.post("/v1/withdrawals", headers=h, json=body).status_code == 422          # key required
    k = key()
    r1 = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": k}, json=body)
    assert r1.status_code == 201, r1.text
    r2 = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": k}, json=body)
    assert r2.status_code == 201 and r2.json() == r1.json() and r2.headers["idempotent-replayed"] == "true"
    assert len(world.withdrawals) == 1 and world.fee_balance(u["id"]) == 60 * USD
    r3 = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": k}, json={"amount": "41", "to_address": W1})
    assert r3.status_code == 409
    # insufficient → 402, and the SAME key works after a top-up (the claim rolled back with the failure)
    k2 = key()
    big = {"amount": "100", "to_address": W1}
    r4 = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": k2}, json=big)
    assert r4.status_code == 402 and r4.json()["error"]["code"] == "insufficient_balance"
    world.credit(u["id"], 100 * USD)
    assert c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": k2}, json=big).status_code == 201
    assert any(a["kind"] == "withdrawal_requested" for a in world.alerts)
    assert any(a["action"] == "withdrawal.request" for a in world.audit)


def test_withdrawal_rules():
    world, svc, c = build()
    _user_with_wallet(world, balance=50 * USD, withdrawable=False)
    h = {**login(svc, world), "Idempotency-Key": key()}
    r = c.post("/v1/withdrawals", headers=h, json={"amount": "20", "to_address": W1})
    assert r.status_code == 402 and r.json()["error"]["details"]["withdrawable_micro"] == 0   # card money
    r = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": key()}, json={"amount": "20", "to_address": W2})
    assert r.status_code == 403                                                            # unverified wallet
    r = c.post("/v1/withdrawals", headers={**h, "Idempotency-Key": key()}, json={"amount": "20.1234567",
                                                                                  "to_address": W1})
    assert r.status_code == 422                                                            # > 6 decimals


def test_payouts_disabled_in_launch_phase():
    world, svc, c = build(make_settings(launch_phase="public", payouts_enabled=False))
    _user_with_wallet(world, balance=50 * USD)
    r = c.post("/v1/withdrawals", headers={**login(svc, world), "Idempotency-Key": key()},
               json={"amount": "20", "to_address": W1})
    assert r.status_code == 403 and r.json()["error"]["details"]["reason"] == "payouts_disabled"


def _subscribe_world(c_world, svc, *, price=20 * USD, ps=1000, balance=50 * USD):
    world = c_world
    creator = world.add_user("fb-creator", role="creator")
    st, ver = world.add_strategy("alpha", price=price, profit_share_bps=ps, in_house=False, owner=creator["id"],
                                 markets=("BTC",))
    u = _user_with_wallet(world, balance=balance)
    world.add_agent(u["id"], W1)
    return u, st, creator


def _ack(c, h, st):
    body = {"consents": [{"doc": "subscription_ack", "doc_version": DEFAULT_LEGAL_VERSIONS["subscription_ack"],
                          "context": "subscribe", "strategy_id": st["id"], "doc_text_sha256": "c" * 64}]}
    assert c.post("/v1/consents", headers=h, json=body).status_code == 200


def test_subscribe_happy_path_charges_and_splits():
    world, svc, c = build()
    u, st, creator = _subscribe_world(world, svc)
    h = login(svc, world)
    body = {"strategy_id": st["id"], "trading_address": W1, "allocation": "500", "max_leverage_x100": 200,
            "expected_price_monthly_micro": 20 * USD}
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json=body)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "subscription_ack_required"
    _ack(c, h, st)
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["charged_micro"] == 20 * USD and out["fee_balance_micro"] == 30 * USD
    assert out["subscription"]["status"] == "active"
    assert world.balances[f"creator:{creator['id']}:payable"] == -19_400_000
    assert world.balances["platform:revenue:subscription"] == -600_000
    # a second strategy on another verified wallet exceeds the free plan (1 active strategy)
    st2, _ = world.add_strategy("beta", price=0, profit_share_bps=0, in_house=True)
    world.add_wallet(u["id"], W2)
    world.add_agent(u["id"], W2)
    _ack(c, h, st2)
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()},
               json={**body, "strategy_id": st2["id"], "trading_address": W2, "expected_price_monthly_micro": 0})
    assert r.status_code == 403 and r.json()["error"]["details"]["plan"] == "free"


def test_subscribe_checks_agent_builder_leverage_terms():
    world, svc, c = build()
    u, st, _ = _subscribe_world(world, svc)
    h = login(svc, world)
    _ack(c, h, st)
    base = {"strategy_id": st["id"], "trading_address": W1, "allocation": "500", "max_leverage_x100": 200}
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json={**base, "max_leverage_x100": 500})
    assert r.status_code == 422                                              # version MAX_LEVERAGE = 2
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()},
               json={**base, "expected_price_monthly_micro": 1})
    assert r.status_code == 409                                              # terms changed
    svc.hl.builder_fee = 50
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json=base)
    assert r.status_code == 409                                              # builder approval too low
    svc.hl.builder_fee = 100
    for a in world.agents.values():
        a["status"] = "revoked"
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json=base)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "agent_not_active"
    r = c.post("/v1/subscriptions", headers={**login(svc, world, stale=True), "Idempotency-Key": key()}, json=base)
    assert r.status_code == 401                                              # step-up


def test_free_strategy_needs_reserve_but_charges_nothing():
    world, svc, c = build()
    silver, _ = world.add_strategy("silver", price=0, profit_share_bps=0, in_house=True)
    u = _user_with_wallet(world, balance=0)
    world.add_agent(u["id"], W1)
    h = login(svc, world)
    _ack(c, h, silver)
    body = {"strategy_id": silver["id"], "trading_address": W1, "allocation": "200", "max_leverage_x100": 100}
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json=body)
    assert r.status_code == 402                                              # $10 reserve for the 1.5 % share
    world.credit(u["id"], 10 * USD)
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()}, json=body)
    assert r.status_code == 201 and r.json()["charged_micro"] == 0
    assert world.fee_balance(u["id"]) == 10 * USD


def test_cancel_requires_positions_choice():
    world, svc, c = build()
    silver, _ = world.add_strategy("silver", price=0, profit_share_bps=0, in_house=True)
    u = _user_with_wallet(world, balance=10 * USD)
    world.add_agent(u["id"], W1)
    h = login(svc, world)
    _ack(c, h, silver)
    r = c.post("/v1/subscriptions", headers={**h, "Idempotency-Key": key()},
               json={"strategy_id": silver["id"], "trading_address": W1, "allocation": "200", "max_leverage_x100": 100})
    sid = r.json()["subscription"]["id"]
    assert c.request("DELETE", f"/v1/subscriptions/{sid}", headers=h).status_code == 422
    assert c.request("DELETE", f"/v1/subscriptions/{sid}", headers=h, json={"positions": "maybe"}).status_code == 422
    r = c.request("DELETE", f"/v1/subscriptions/{sid}", headers=h, json={"positions": "close"})
    assert r.status_code == 200 and r.json()["status"] == "closing"
    r = c.request("DELETE", f"/v1/subscriptions/{sid}", headers=h, json={"positions": "leave"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert c.request("DELETE", f"/v1/subscriptions/{sid}", headers=h, json={"positions": "leave"}).status_code == 409
    assert any(a["action"] == "subscription.cancel" and a["payload"]["positions"] == "close" for a in world.audit)


def test_stripe_webhook_signature_and_idempotent_credit():
    world, svc, c = build()
    u = world.add_user("fb-user")
    event = {"id": "evt_1", "type": "credit", "user_id": u["id"], "amount_micro": 25 * USD, "pi": "pi_abc"}
    raw = json.dumps(event).encode()
    assert c.post("/v1/webhooks/stripe", content=raw, headers={"Stripe-Signature": "forged"}).status_code == 400
    assert c.post("/v1/webhooks/stripe", content=raw).status_code == 422
    for _ in range(2):   # redelivery is a no-op
        r = c.post("/v1/webhooks/stripe", content=raw, headers={"Stripe-Signature": "good"})
        assert r.status_code == 200 and r.json() == {"received": True}
    assert world.fee_balance(u["id"]) == 25 * USD
    assert world.deposits["pi_abc"]["status"] == "credited" and world.deposits["pi_abc"]["withdrawable"] is False


def test_agent_create_rotate_and_confirm():
    world, svc, c = build()
    u = world.add_user("fb-user")
    h = login(svc, world)
    body = {"master_address": W1, "signature_chain_id": "0xa4b1"}
    assert c.post("/v1/agents", headers=h, json=body).status_code == 403              # wallet not verified
    world.add_wallet(u["id"], W1)
    r = c.post("/v1/agents", headers=h, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    agent_id, agent_addr = out["agent"]["id"], out["agent"]["agent_address"]
    assert out["approve_agent"]["typed_data"]["primaryType"] == "HyperliquidTransaction:ApproveAgent"
    assert out["approve_builder_fee"] is not None and out["required_builder_fee_tenths_bp"] == 100
    assert "key_ciphertext" not in r.text and "sealed" not in r.text
    assert c.post(f"/v1/agents/{agent_id}/confirm", headers=h).status_code == 422      # not on-chain yet
    svc.hl.agents[W1] = [{"address": agent_addr, "name": "aijalon", "validUntil": None}]
    r = c.post(f"/v1/agents/{agent_id}/confirm", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert c.post("/v1/agents", headers=h, json=body).status_code == 409               # active exists
    r = c.post("/v1/agents", headers=h, json={**body, "rotate": True})
    assert r.status_code == 201 and world.agents[agent_id]["status"] == "rotated"


# ================================================================================================= admin
def _admins(world):
    a1 = world.add_user("fb-admin1", role="admin", email="a1@aijalon.trade")
    a2 = world.add_user("fb-admin2", role="admin", email="a2@aijalon.trade")
    return a1, a2


def test_payout_two_admins_required():
    world, svc, c = build()
    u = _user_with_wallet(world, balance=100 * USD)
    a1, a2 = _admins(world)
    r = c.post("/v1/withdrawals", headers={**login(svc, world), "Idempotency-Key": key()},
               json={"amount": "30", "to_address": W1})
    wid = r.json()["id"]
    h1, h2 = login(svc, world, "fb-admin1"), login(svc, world, "fb-admin2")
    r = c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h1)
    assert r.status_code == 200 and r.json()["status"] == "approved_1"
    assert c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h1).status_code == 403
    r = c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=h2)
    assert r.status_code == 200 and r.json()["status"] == "approved_2"
    r = c.post(f"/v1/admin/payouts/withdrawal/{wid}/typed-data", headers=h1, json={"signature_chain_id": "0xa4b1"})
    assert r.status_code == 200 and r.json()["payload"]["action"]["destination"] == W1
    stale = login(svc, world, "fb-admin2", stale=True)
    assert c.post(f"/v1/admin/payouts/withdrawal/{wid}/approve", headers=stale).status_code == 401


def test_kill_switch_engage_now_lift_needs_second_admin():
    world, svc, c = build()
    a1, a2 = _admins(world)
    h1, h2 = login(svc, world, "fb-admin1"), login(svc, world, "fb-admin2")
    r = c.post("/v1/admin/flags", headers=h1, json={"key": "new_entries_paused:xyz:SILVER", "value": True,
                                                    "reason": "oracle divergence"})
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert world.flags["new_entries_paused:xyz:SILVER"]["value"] is True
    r = c.post("/v1/admin/flags", headers=h1, json={"key": "new_entries_paused:xyz:SILVER", "value": False,
                                                    "reason": "resolved, lifting"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert world.flags["new_entries_paused:xyz:SILVER"]["value"] is True
    assert c.post("/v1/admin/flags/new_entries_paused:xyz:SILVER/approve", headers=h1,
                  json={"reason": "self approve"}).status_code == 403
    r = c.post("/v1/admin/flags/new_entries_paused:xyz:SILVER/approve", headers=h2, json={"reason": "checked ok"})
    assert r.status_code == 200 and world.flags["new_entries_paused:xyz:SILVER"]["value"] is False
    bad = c.post("/v1/admin/flags", headers=h1, json={"key": "drop_tables", "value": True, "reason": "nope nope"})
    assert bad.status_code == 422
