"""Security-fix round, account / admin side, against a REAL migrated Postgres (0001–0012), API statements AS app_api.

  F17    app_api cannot grant or remove `admin` (trigger); promote_admin() (SECURITY DEFINER) can
  F6     pending admin changes can be cancelled (no checker) and never approved afterwards
  F12    'user_suspend' change kind (suspending another admin needs a second admin)
  F18    LIKE wildcards escaped in the admin user search
  F16    public posts hide unlisted/draft strategies and suspended creators
  F14    review eligibility = time actually subscribed
  F5     security hold + wallet age → payout holds; wallet age = FIRST verification
  F7/M2  device / network identity, self-referral check + flag with the real store

Run: AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/<migrated db> python3 -m unittest \
     tests.test_fix_api_account_db   (from backend/)
"""
from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

from test_fix_api_money_db import DB_URL, RUN, Svc, _addr  # noqa: E402

if RUN:
    from test_fix_api_money_db import ApiRoleRunner, DbError  # noqa: E402

UTC = timezone.utc


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (migrated through 0011) and psql")
class FixAccountDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.api.store import SqlStore
        cls.db = ApiRoleRunner(DB_URL)
        cls.su = ApiRoleRunner(DB_URL, "postgres")
        cls.mig = ApiRoleRunner(DB_URL, "app_migrator")
        cls.store = SqlStore()
        cls.now = datetime.now(UTC)
        cls.svc = Svc(cls.store, cls.now)

    def user(self, tag: str = "", email: str | None = None) -> dict:
        t = uuid.uuid4().hex[:10]
        return self.store.create_user(self.db, firebase_uid=f"fa{tag}{t}", email=email or f"{tag}{t}@x.io",
                                      display_name="U", referral_code=f"A{t}", referred_by=None, mfa_enrolled=True)

    # ------------------------------------------------------------------------------------------------ F17
    def test_app_api_cannot_grant_admin(self) -> None:
        u = self.user("r")
        uid = str(u["id"])
        self.store.set_role(self.db, uid, "creator")                       # the creator agreement still works
        with self.assertRaises(DbError) as cm:
            self.store.set_role(self.db, uid, "admin")
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        with self.assertRaises(DbError) as cm:                             # and not via INSERT either
            self.db.fetchall("""INSERT INTO users (firebase_uid, role) VALUES (:f, 'admin') RETURNING id""",
                             {"f": "evil" + uuid.uuid4().hex})
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        with self.assertRaises(DbError):                                   # app_api may not call the promotion
            self.db.fetchall("SELECT promote_admin(:e) AS id", {"e": u["email"]})
        got = self.mig.fetchall("SELECT promote_admin(:e) AS id", {"e": u["email"]})
        self.assertEqual(str(got[0]["id"]), uid)
        self.assertEqual(self.store.get_user(self.db, uid)["role"], "admin")
        with self.assertRaises(DbError) as cm:                             # nor remove it
            self.store.set_role(self.db, uid, "user")
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        audit = self.su.fetchall("SELECT count(*) AS n FROM audit_log WHERE action = 'user.promote_admin' AND target = :t",
                                 {"t": f"user:{uid}"})
        self.assertEqual(audit[0]["n"], 1)

    # ------------------------------------------------------------------------------------------------ F6 / F12
    def test_admin_changes_cancelled_and_user_suspend_kind(self) -> None:
        s, db = self.store, self.db
        a1, a2 = str(self.user("a")["id"]), str(self.user("a")["id"])
        target = f"strategy:{uuid.uuid4()}"
        ch = s.insert_change(db, kind="strategy_list", target=target, payload={"version_id": str(uuid.uuid4())},
                             reason="list it please", maker=a1)
        out = s.cancel_pending_changes(db, target, "superseded: strategy delisted by admin", self.now)
        self.assertEqual([str(r["id"]) for r in out], [str(ch["id"])])
        self.assertIsNone(s.decide_change(db, str(ch["id"]), status="approved", checker=a2, now=self.now,
                                          decision_reason="approve stale"))          # never approvable later
        self.assertEqual(s.get_change(db, str(ch["id"]), for_update=False)["status"], "cancelled")
        self.assertIsNone(s.open_change_for(db, "strategy_list", target))
        sus = s.insert_change(db, kind="user_suspend", target=f"user:{a2}", payload={}, reason="compromised admin",
                              maker=a1)
        row = s.decide_change(db, str(sus["id"]), status="approved", checker=str(self.user("a")["id"]), now=self.now,
                              decision_reason="confirmed by second admin")
        self.assertEqual(row["status"], "approved")

    # ------------------------------------------------------------------------------------------------ F18
    def test_user_search_escapes_like_wildcards(self) -> None:
        t = uuid.uuid4().hex[:8]
        exact = self.user("s", email=f"a_b{t}@x.io")
        other = self.user("s", email=f"axb{t}@x.io")
        ids = {str(r["id"]) for r in self.store.search_users(self.db, f"a_b{t}", 50, None)}
        self.assertIn(str(exact["id"]), ids)
        self.assertNotIn(str(other["id"]), ids)                            # '_' is not a wildcard any more
        self.assertEqual(self.store.search_users(self.db, "%", 50, None), [])

    # ------------------------------------------------------------------------------------------------ F16
    def test_public_posts_hide_unlisted_strategies_and_suspended_creators(self) -> None:
        s, db = self.store, self.db
        creator, banned = str(self.user("c")["id"]), str(self.user("c")["id"])
        st = s.insert_strategy(db, owner_user_id=creator, slug=f"pp-{uuid.uuid4().hex[:10]}", name="P",
                               description=None, markets=["BTC"], timeframe="1d", price_monthly_micro=0,
                               profit_share_bps=0)
        draft_post = s.insert_post(db, creator_id=creator, strategy_id=str(st["id"]), title="Draft strat post",
                                   body="b", price_micro=0, now=self.now)
        general = s.insert_post(db, creator_id=creator, strategy_id=None, title="General", body="b", price_micro=0,
                                now=self.now)
        gone = s.insert_post(db, creator_id=banned, strategy_id=None, title="Banned", body="b", price_micro=0,
                             now=self.now)
        s.set_user_status(db, banned, "suspended")
        ids = {str(r["id"]) for r in s.list_public_posts(db, strategy_slug=None, limit=50, cursor=None)}
        self.assertIn(str(general["id"]), ids)
        self.assertNotIn(str(draft_post["id"]), ids)
        self.assertNotIn(str(gone["id"]), ids)
        s.set_strategy_status(db, str(st["id"]), "listed")
        ids = {str(r["id"]) for r in s.list_public_posts(db, strategy_slug=None, limit=50, cursor=None)}
        self.assertIn(str(draft_post["id"]), ids)

    # ------------------------------------------------------------------------------------------------ F14
    def test_review_eligibility_counts_time_subscribed(self) -> None:
        s, db = self.store, self.db
        owner, u = str(self.user("o")["id"]), str(self.user("v")["id"])
        st = s.insert_strategy(db, owner_user_id=owner, slug=f"rv-{uuid.uuid4().hex[:10]}", name="R",
                               description=None, markets=["BTC"], timeframe="1d", price_monthly_micro=0,
                               profit_share_bps=0)
        sid = str(st["id"])
        ver = s.insert_version(db, strategy_id=sid, version=1, code_hash="f" * 64, code_ciphertext=b"\x00",
                               params={}, markets=["BTC"], timeframe="1d", lookback=300, max_leverage=1, backtest={})
        addr = _addr(int(uuid.uuid4().hex[:12], 16))
        sub = s.insert_subscription(db, user_id=u, strategy_id=sid, version_id=str(ver["id"]), trading_address=addr,
                                    master_address=addr, allocation_micro=10_000_000, max_leverage_x100=100,
                                    status="active", current_period_end=self.now)
        # subscribed 40 days ago, cancelled one minute later: "30 days since first subscribe" said eligible
        self.su.fetchall("""UPDATE subscriptions SET created_at = :c, status = 'cancelled', cancelled_at = :x
                             WHERE id = CAST(:id AS uuid)""",
                         {"c": (self.now - timedelta(days=40)).isoformat(),
                          "x": (self.now - timedelta(days=40) + timedelta(minutes=1)).isoformat(), "id": str(sub["id"])})
        self.assertIsNotNone(s.earliest_subscription(db, u, sid))
        self.assertLess(s.subscribed_seconds(db, u, sid, self.now), 30 * 86400)
        self.su.fetchall("UPDATE subscriptions SET cancelled_at = :x WHERE id = CAST(:id AS uuid)",
                         {"x": (self.now - timedelta(days=5)).isoformat(), "id": str(sub["id"])})
        self.assertGreaterEqual(s.subscribed_seconds(db, u, sid, self.now), 35 * 86400 - 60)

    # ------------------------------------------------------------------------------------------------ F5
    def test_security_hold_and_wallet_age(self) -> None:
        from app.api import billing_ops
        s, db = self.store, self.db
        uid = str(self.user("h")["id"])
        w = _addr(int(uuid.uuid4().hex[:12], 16))
        first = self.now - timedelta(days=10)
        s.upsert_verified_wallet(db, uid, w, first)
        s.upsert_verified_wallet(db, uid, w, self.now)                    # re-verification keeps the FIRST time
        self.assertEqual(s.verified_wallet(db, uid, w)["verified_at"], first)
        reasons, ctx = billing_ops.payout_holds(db, self.svc, user_id=uid, to_address=w)
        self.assertEqual(reasons, [])
        fresh = _addr(int(uuid.uuid4().hex[:12], 16))
        s.upsert_verified_wallet(db, uid, fresh, self.now - timedelta(hours=2))
        self.assertEqual(billing_ops.payout_holds(db, self.svc, user_id=uid, to_address=fresh)[0],
                         ["payout_address_hold"])
        s.extend_security_hold(db, uid, self.now + timedelta(hours=48))
        s.extend_security_hold(db, uid, self.now + timedelta(hours=1))     # never shortens
        self.store.insert_audit(db, actor=f"user:{uid}", action="auth.mfa_changed", target=f"user:{uid}", payload={},
                                ip_hash=None)
        reasons, ctx = billing_ops.payout_holds(db, self.svc, user_id=uid, to_address=w)
        self.assertEqual(reasons, ["security_hold"])
        self.assertEqual(ctx["security_hold_until"], self.now + timedelta(hours=48))
        self.assertEqual([e["action"] for e in ctx["events"]], ["auth.mfa_changed"])
        from app.errors import Forbidden
        with self.assertRaises(Forbidden) as cm:
            billing_ops.require_no_payout_hold(db, self.svc, user_id=uid, to_address=w)
        self.assertEqual(cm.exception.details["reason"], "security_hold")

    # ------------------------------------------------------------------------------------------------ F7 / M2
    def test_self_referral_identity_and_flag(self) -> None:
        from app.api import login_events
        from app.api.referral_guard import flag_self_referral, self_referral_check
        s, db = self.store, self.db
        ref, alt = self.user("ref"), self.user("alt")
        rid, aid = str(ref["id"]), str(alt["id"])
        pepper = b"p" * 32
        dev, _ = login_events.device_key({"x-device-id": "D" * 24, "user-agent": "UA"}, pepper)
        net = login_events.network_hash("203.0.113.77", pepper)
        ua_only, _ = login_events.device_key({"user-agent": "UA"}, pepper)
        s.record_device(db, rid, ua_only, "Chrome on macOS")      # user-agent fallback: never used for referrals
        s.record_device(db, rid, dev, "Chrome on macOS")
        s.mark_device_id(db, rid, dev)
        self.assertTrue(s.record_ip_net(db, rid, net))
        self.assertFalse(s.record_ip_net(db, rid, net))
        ident = s.referral_identity(db, rid, self.now - timedelta(days=30))
        self.assertEqual((ident["devices"], ident["networks"]), ([dev], [net]))
        # sign-up of the alt account from the SAME device → refused outright
        blocked, reasons = self_referral_check(db, self.svc, referrer=ref, referee={"id": None},
                                               referee_devices=[dev],
                                               referee_networks=[login_events.network_hash("203.0.113.9", pepper)])
        self.assertTrue(blocked)
        self.assertEqual(set(reasons), {"same_device", "same_network"})
        # another device on the same /24 → allowed but flagged (no reward until ops clears it)
        other_dev, _ = login_events.device_key({"x-device-id": "E" * 24}, pepper)
        blocked, reasons = self_referral_check(db, self.svc, referrer=ref, referee={"id": None},
                                               referee_devices=[other_dev],
                                               referee_networks=[login_events.network_hash("203.0.113.9", pepper)])
        self.assertEqual((blocked, reasons), (False, ("same_network",)))
        s.set_device_fp_hash(db, aid, other_dev)
        flag_self_referral(db, self.svc, referee_id=aid, referrer_id=rid, reasons=reasons, where="signup")
        got = self.su.fetchall("SELECT referral_flagged_at, referral_flag_reason, device_fp_hash FROM users "
                               "WHERE id = CAST(:u AS uuid)", {"u": aid})[0]
        self.assertIsNotNone(got["referral_flagged_at"])
        self.assertIn("same_network", got["referral_flag_reason"])
        self.assertEqual(got["device_fp_hash"], other_dev)
        self.assertEqual(self.svc.notes[-1]["kind"], "self_referral_suspected")


if __name__ == "__main__":
    unittest.main()
