"""Integration tests for app/api/store.py (+ API ledger postings) against a REAL migrated Postgres 16.

Every statement runs AS THE `app_api` ROLE, so these tests also prove the API never touches a column or table its
DB role cannot (e.g. agent_keys.key_ciphertext, strategy_versions.code_ciphertext) and that all SQL parses,
type-checks and satisfies the schema's constraints/triggers.

Runs when AIJALON_TEST_DATABASE_URL points at a migrated database (0001–0004) and `psql` is on PATH, e.g.
    createdb aj_api && python backend/scripts/migrate.py --database-url postgresql://postgres@localhost:55432/aj_api
    AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/aj_api python -m pytest backend/tests/test_api_store_db.py
The connecting user must be able to `SET ROLE app_api` (a superuser or a member of app_api).
Bind parameters are rendered as SQL literals by the test runner (never in production).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

DB_URL = os.environ.get("AIJALON_TEST_DATABASE_URL", "")


def _psql_ok() -> bool:
    try:
        return subprocess.run(["psql", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


RUN = bool(DB_URL) and _psql_ok()

if RUN:
    from psql_runner import PsqlRunner  # noqa: E402  (backend/tests/db)

    from app.db.engine import DbError  # noqa: E402

    class ApiRoleRunner(PsqlRunner):
        """PsqlRunner + SET ROLE app_api, RETURNING rows for INSERT/UPDATE, and literal rendering of
        bytes / lists / datetimes the way psycopg would bind them."""

        def __init__(self, url: str, role: str = "app_api") -> None:
            super().__init__(url)
            self.role = role

        @staticmethod
        def lit(v: Any) -> str:
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "TRUE" if v else "FALSE"
            if isinstance(v, int):
                return str(v)
            if isinstance(v, (bytes, bytearray)):
                return "'\\x" + bytes(v).hex() + "'::bytea"
            if isinstance(v, (list, tuple)):
                return "ARRAY[" + ",".join(ApiRoleRunner.lit(x) for x in v) + "]::text[]" if v else "ARRAY[]::text[]"
            if isinstance(v, datetime):
                return "'" + v.isoformat() + "'::timestamptz"
            if isinstance(v, (date, Decimal)):
                return "'" + str(v) + "'"
            if isinstance(v, dict):
                v = json.dumps(v)
            return "'" + str(v).replace("'", "''") + "'"

        def render(self, sql: str, params: Mapping[str, Any] | None) -> str:
            params = dict(params or {})
            bind = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)(?!:)")

            def sub(m: re.Match[str]) -> str:
                if m.group(1) not in params:
                    raise KeyError(f"missing bind parameter {m.group(1)!r}")
                return self.lit(params[m.group(1)])

            return bind.sub(sub, sql)

        @staticmethod
        def _final_select_at(body: str) -> int:
            """Index of the top-level SELECT that follows a WITH list (paren depth 0, outside quotes)."""
            depth, i, quote = 0, 0, False
            last = -1
            while i < len(body):
                ch = body[i]
                if quote:
                    if ch == "'":
                        quote = False
                elif ch == "'":
                    quote = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif depth == 0 and body[i:i + 6].upper() == "SELECT" and (i == 0 or not body[i - 1].isalnum()):
                    last = i
                i += 1
            return last

        def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
            body = self.render(sql, params).strip().rstrip(";")
            head = body.lstrip().split(None, 1)[0].upper()
            modifying = re.search(r"\b(INSERT|UPDATE|DELETE)\b", body, re.IGNORECASE) is not None
            returns_rows = head in ("SELECT", "WITH") or re.search(r"\bRETURNING\b", body, re.IGNORECASE) is not None
            if head in ("INSERT", "UPDATE", "DELETE") and returns_rows:
                body = f"WITH q AS ({body}) SELECT coalesce(json_agg(q), '[]') FROM q"
            elif head == "WITH" and modifying:
                at = self._final_select_at(body)
                body = f"{body[:at].rstrip()}, __final AS ({body[at:]}) SELECT coalesce(json_agg(__final), '[]') FROM __final"
            elif returns_rows:
                body = f"SELECT coalesce(json_agg(q), '[]') FROM ({body}) q"
            script = f"\\set VERBOSITY verbose\nSET ROLE {self.role};\n{body};\n"
            r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-f", "-"],
                               input=script, capture_output=True, text=True)
            if r.returncode != 0:
                m = re.search(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)", r.stderr)
                raise DbError(m.group(1) if m else None, (m.group(2) if m else r.stderr).strip())
            if not returns_rows:
                return []
            out = r.stdout.strip()
            return json.loads(out) if out else []


class _Svc:
    """Minimal svc for ledger_ops: real ledger service + the store under test (no FastAPI needed)."""

    def __init__(self, store: Any) -> None:
        from app.api import ledger_ops
        from app.ledger import service

        class Ledger:
            def ensure_account(self, conn: Any, code: str) -> None:
                kind, nn, owner = ledger_ops.account_spec(code)
                service.ensure_account(conn, code, kind, owner, non_negative=nn)

            def post(self, conn: Any, *, idempotency_key: str, kind: str, memo: str, entries: list, created_by: str) -> str:
                return service.post_transaction(conn, idempotency_key, kind, memo, entries, created_by).id

            def balance(self, conn: Any, code: str) -> int:
                return service.get_balance(conn, code)

        class Domain:
            def subscription_split(self, price: int) -> tuple[int, int]:
                from app.domain.fees import subscription_split
                return subscription_split(price)

            def post_sale_split(self, price: int) -> tuple[int, int]:
                from app.domain.fees import post_sale_split
                return post_sale_split(price)

            def plan_price(self, plan: str) -> int:
                from app.domain.fees import plan_price
                return plan_price(plan)

        self.store = store
        self.ledger = Ledger()
        self.domain = Domain()
        self.notifier = None


def _addr(n: int) -> str:
    return "0x" + f"{n:040x}"


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (migrated through 0004) and psql")
class ApiStoreDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.api.store import SqlStore
        cls.db = ApiRoleRunner(DB_URL)
        cls.store = SqlStore()
        cls.svc = _Svc(cls.store)
        cls.now = datetime.now(timezone.utc)
        tag = uuid.uuid4().hex[:8]
        s = cls.store
        cls.a = s.create_user(cls.db, firebase_uid=f"fbA{tag}", email=f"a{tag}@x.io", display_name="A",
                              referral_code=f"A{tag}", referred_by=None, mfa_enrolled=True)
        cls.b = s.create_user(cls.db, firebase_uid=f"fbB{tag}", email=f"b{tag}@x.io", display_name="B",
                              referral_code=f"B{tag}", referred_by=None, mfa_enrolled=True)
        cls.c = s.create_user(cls.db, firebase_uid=f"fbC{tag}", email=f"c{tag}@x.io", display_name="C",
                              referral_code=f"C{tag}", referred_by=None, mfa_enrolled=True)
        cls.tag = tag
        cls.seed = int(tag, 16)

    # ------------------------------------------------------------------------------------------------ users
    def test_users_and_referrals(self) -> None:
        s, db = self.store, self.db
        self.assertTrue(self.a["_created"])
        again = s.create_user(db, firebase_uid=self.a["firebase_uid"], email=None, display_name=None,
                              referral_code="ZZ" + self.tag, referred_by=None, mfa_enrolled=True)
        self.assertFalse(again["_created"])
        self.assertEqual(str(again["id"]), str(self.a["id"]))
        clash = s.create_user(db, firebase_uid="other" + self.tag, email=None, display_name=None,
                              referral_code=self.a["referral_code"], referred_by=None, mfa_enrolled=True)
        self.assertIsNone(clash)
        self.assertEqual(s.get_user_by_referral_code(db, self.a["referral_code"])["firebase_uid"], self.a["firebase_uid"])
        self.assertEqual(s.get_user(db, str(self.a["id"]), for_update=True)["role"], "user")
        s.lock_user(db, str(self.a["id"]))
        s.update_display_name(db, str(self.a["id"]), "Alice")
        s.set_country_attested(db, str(self.a["id"]), "MY")
        s.set_role(db, str(self.a["id"]), "creator")
        s.set_plan(db, str(self.a["id"]), "pro", self.now + timedelta(days=30))
        u = s.get_user_by_firebase_uid(db, self.a["firebase_uid"])
        self.assertEqual((u["display_name"], u["country_attested"], u["role"], u["plan"]), ("Alice", "MY", "creator", "pro"))
        self.assertTrue(s.bind_referrer(db, user_id=str(self.c["id"]), referrer_id=str(self.a["id"])))
        self.assertFalse(s.bind_referrer(db, user_id=str(self.c["id"]), referrer_id=str(self.b["id"])))
        self.assertFalse(s.record_login_country(db, str(self.b["id"]), "MY"))
        self.assertTrue(s.record_login_country(db, str(self.b["id"]), "SG"))
        self.assertFalse(s.record_login_country(db, str(self.b["id"]), "SG"))
        self.assertTrue(s.search_users(db, self.b["email"][:6], 10, None))
        stats = s.referral_stats(db, str(self.a["id"]), self.now - timedelta(days=30))
        self.assertEqual(int(stats["total"]), 1)

    def test_consents_and_nonces(self) -> None:
        s, db, uid = self.store, self.db, str(self.b["id"])
        for d in ("terms", "risk"):
            s.insert_consent(db, user_id=uid, doc=d, version="2026-09-30", doc_text_sha256="a" * 64,
                             context="site_entry", strategy_id=None, ip_hash="h", ua_hash=None)
        self.assertEqual(s.accepted_consents(db, uid), {"terms": "2026-09-30", "risk": "2026-09-30"})
        st = s.get_public_strategy(db, "silver")
        self.assertEqual(s.get_strategy(db, str(st["id"]))["price_monthly_micro"], 0)
        s.insert_consent(db, user_id=uid, doc="subscription_ack", version="2026-09-30", doc_text_sha256="b" * 64,
                         context="subscribe", strategy_id=str(st["id"]), ip_hash=None, ua_hash=None)
        self.assertIsNotNone(s.recent_subscription_ack(db, user_id=uid, strategy_id=str(st["id"]), version="2026-09-30",
                                                       since=self.now - timedelta(minutes=30)))
        self.assertIsNone(s.recent_subscription_ack(db, user_id=uid, strategy_id=str(st["id"]), version="2099-01-01",
                                                    since=self.now - timedelta(minutes=30)))
        self.assertNotIn("subscription_ack", s.accepted_consents(db, uid))
        nonce = "N" + uuid.uuid4().hex
        s.create_wallet_nonce(db, user_id=uid, nonce=nonce, expires_at=self.now + timedelta(minutes=10))
        self.assertFalse(s.consume_wallet_nonce(db, user_id=str(self.a["id"]), nonce=nonce, now=self.now))
        self.assertTrue(s.consume_wallet_nonce(db, user_id=uid, nonce=nonce, now=self.now))
        self.assertFalse(s.consume_wallet_nonce(db, user_id=uid, nonce=nonce, now=self.now))

    def test_wallets_agents_builder(self) -> None:
        s, db, uid = self.store, self.db, str(self.b["id"])
        w = _addr(self.seed * 10 + 1)
        row = s.upsert_verified_wallet(db, uid, w, self.now)
        self.assertEqual(str(row["user_id"]), uid)
        other = s.upsert_verified_wallet(db, str(self.c["id"]), w, self.now)   # never re-binds
        self.assertEqual(str(other["user_id"]), uid)
        self.assertIsNotNone(s.verified_wallet(db, uid, w))
        self.assertEqual(s.user_for_verified_wallet(db, w), uid)
        self.assertEqual(len(s.list_wallets(db, uid)), 1)
        self.assertEqual(str(s.get_wallet(db, w)["user_id"]), uid)
        agent = s.insert_agent(db, user_id=uid, master=w, agent_address=_addr(self.seed * 10 + 2), agent_name="aijalon",
                               key_ciphertext=b"\x01\x02sealed", kms_key_version="v1")
        self.assertEqual(agent["status"], "pending_approval")
        self.assertNotIn("key_ciphertext", agent)
        self.assertEqual(len(s.live_agents_for_master(db, w)), 1)
        s.set_agent_status(db, str(agent["id"]), "active", self.now)
        self.assertIsNotNone(s.active_agent_for_master(db, uid, w))
        self.assertEqual(s.get_agent(db, str(agent["id"]), uid, for_update=True)["status"], "active")
        self.assertEqual(len(s.list_agents(db, uid)), 1)
        with self.assertRaises(DbError) as cm:  # app_api can never read the ciphertext back
            db.fetchall("SELECT key_ciphertext FROM agent_keys LIMIT 1")
        self.assertEqual(cm.exception.sqlstate, "42501")
        ba = s.insert_builder_approval(db, user_id=uid, master=w, rate=100, now=self.now)
        self.assertEqual(ba["max_fee_rate_tenths_bp"], 100)

    # ------------------------------------------------------------------------------------------------ strategies
    def _creator_strategy(self) -> tuple[dict, dict]:
        s, db, uid = self.store, self.db, str(self.a["id"])
        st = s.insert_strategy(db, owner_user_id=uid, slug=f"t-{uuid.uuid4().hex[:10]}", name="Test", description="d",
                               markets=["BTC", "xyz:SILVER"], timeframe="1d", price_monthly_micro=20_000_000,
                               profit_share_bps=1000)
        self.assertIsNotNone(st)
        ver = s.insert_version(db, strategy_id=str(st["id"]), version=s.next_version_number(db, str(st["id"])),
                               code_hash="c" * 64, code_ciphertext=b"\x00sealed-code", params={"source": "python"},
                               markets=["BTC"], timeframe="1d", lookback=300, max_leverage=2,
                               backtest={"period": {"sim_days": 400.5}, "metrics": {"sharpe": None}})
        self.assertEqual(ver["version"], 1)
        self.assertNotIn("code_ciphertext", ver)
        self.assertEqual(s.publish_version(db, str(ver["id"]), self.now), 1)
        self.assertEqual(s.publish_version(db, str(ver["id"]), self.now), 0)
        return st, ver

    def test_strategies_public_and_admin(self) -> None:
        s, db = self.store, self.db
        st, ver = self._creator_strategy()
        sid = str(st["id"])
        self.assertIsNone(s.insert_strategy(db, owner_user_id=str(self.a["id"]), slug=st["slug"], name="dup",
                                            description=None, markets=["BTC"], timeframe="1d",
                                            price_monthly_micro=0, profit_share_bps=0))
        self.assertEqual(str(s.current_versions(db, [sid])[sid]["id"]), str(ver["id"]))
        self.assertEqual(len(s.list_versions(db, sid)), 1)
        self.assertEqual(s.get_version(db, str(ver["id"]))["max_leverage"], 2)
        s.update_strategy_terms(db, sid, name="Renamed", description=None, price_monthly_micro=None, profit_share_bps=500)
        s.set_strategy_price(db, sid, 25_000_000)
        s.set_strategy_status(db, sid, "listed")
        got = s.get_public_strategy(db, st["slug"])
        self.assertEqual((got["name"], got["profit_share_bps"], got["price_monthly_micro"]), ("Renamed", 500, 25_000_000))
        self.assertTrue(any(str(r["id"]) == sid for r in s.list_public_strategies(db, market="BTC", limit=200, cursor=None)))
        self.assertIsNotNone(s.get_public_strategy(db, "silver"))
        self.assertEqual(s.holds(db, [sid]), {})
        events, spans = s.track_record_inputs(db, sid, self.now - timedelta(days=1))
        self.assertEqual((events, spans), ([], []))
        self.assertEqual(int(s.rating_summary(db, sid)["n"]), 0)
        self.assertEqual(s.showcase(db, "silver", self.now), [])
        self.assertTrue(s.list_owned_strategies(db, str(self.a["id"])))
        self.assertTrue(s.admin_list_strategies(db, "listed", 50, None))
        self.assertTrue(s.active_subscribers_by_strategy(db, str(self.a["id"])))
        s.upsert_kyc_pending(db, user_id=str(self.a["id"]), provider="test", provider_ref="ref-" + self.tag)
        self.assertEqual(s.get_kyc(db, str(self.a["id"]))["status"], "pending")
        self.assertEqual(s.set_kyc_status(db, str(self.a["id"]), "approved"), 1)
        self.assertEqual(s.get_kyc(db, str(self.a["id"]))["status"], "approved")

    # ------------------------------------------------------------------------------------------------ subscriptions
    def test_subscription_lifecycle(self) -> None:
        s, db, uid = self.store, self.db, str(self.b["id"])
        st, ver = self._creator_strategy()
        sid = str(st["id"])
        addr = _addr(self.seed * 10 + 5)
        sub = s.insert_subscription(db, user_id=uid, strategy_id=sid, version_id=str(ver["id"]), trading_address=addr,
                                    master_address=addr, allocation_micro=500_000_000, max_leverage_x100=200,
                                    status="active", current_period_end=self.now + timedelta(days=30))
        sub_id = str(sub["id"])
        self.assertEqual(sub["strategy_slug"], st["slug"])
        self.assertEqual(s.count_live_subscriptions(db, uid), 1)
        self.assertEqual(s.total_live_allocation(db, uid), 500_000_000)
        self.assertEqual(s.total_live_allocation(db, uid, exclude_subscription_id=sub_id), 0)
        self.assertGreaterEqual(s.total_live_allocation(db), 500_000_000)
        self.assertIsNotNone(s.live_subscription_on_address(db, addr))
        with self.assertRaises(DbError) as cm:   # one live subscription per trading address
            s.insert_subscription(db, user_id=uid, strategy_id=sid, version_id=str(ver["id"]), trading_address=addr,
                                  master_address=addr, allocation_micro=1, max_leverage_x100=100, status="active",
                                  current_period_end=self.now)
        self.assertEqual(cm.exception.sqlstate, "23505")
        s.update_subscription(db, sub_id, allocation_micro=600_000_000, max_leverage_x100=None, status="paused_user")
        got = s.get_subscription(db, sub_id, uid, for_update=True)
        self.assertEqual((got["allocation_micro"], got["status"]), (600_000_000, "paused_user"))
        self.assertIsNone(s.live_subscription_on_address(db, addr))
        self.assertEqual(s.live_subscription_prices(db, uid), [])
        self.assertIn(addr, s.trading_addresses(db, uid))
        self.assertEqual(s.end_subscription(db, sub_id, positions="close", now=self.now)["status"], "closing")
        self.assertIsNotNone(s.live_subscription_on_address(db, addr))           # closing still occupies it
        self.assertEqual(s.end_subscription(db, sub_id, positions="leave", now=self.now)["status"], "cancelled")
        self.assertIsNone(s.end_subscription(db, sub_id, positions="leave", now=self.now))
        rows = s.list_subscriptions(db, uid, 10, None)
        self.assertEqual(rows[0]["cancel_positions"], "leave")
        self.assertIsNotNone(s.earliest_subscription(db, uid, sid))
        s.upsert_review(db, strategy_id=sid, user_id=uid, rating=4, body="ok", eligible_since=self.now)
        s.upsert_review(db, strategy_id=sid, user_id=uid, rating=5, body="great", eligible_since=self.now)
        self.assertEqual(s.list_reviews(db, sid, 10, None)[0]["rating"], 5)
        self.assertEqual(s.pause_subscriptions_of_strategy(db, sid), 0)

    # ------------------------------------------------------------------------------------------------ money
    def test_money_flows(self) -> None:
        from types import SimpleNamespace

        from app.api import ledger_ops
        s, db, svc = self.store, self.db, self.svc
        uid, creator = str(self.b["id"]), str(self.a["id"])
        ref = "0x" + uuid.uuid4().hex + uuid.uuid4().hex
        s.insert_pending_deposit(db, user_id=uid, method="usdc_hl", external_ref=ref, amount_micro=100_000_000)
        credit = SimpleNamespace(user_id=uid, amount_micro=100_000_000, external_ref=ref,
                                 idempotency_key=f"usdc_hl:{ref}", method="usdc_hl", debit_account="treasury:hl_usdc",
                                 credit_account=ledger_ops.fee_balance(uid), kind="deposit", memo="test",
                                 withdrawable=True, meta={"from": _addr(1), "time_ms": 1})
        dep = ledger_ops.apply_credit(db, svc, credit, actor=f"user:{uid}")
        self.assertEqual((dep["status"], dep["withdrawable"]), ("credited", True))
        dep2 = ledger_ops.apply_credit(db, svc, credit, actor=f"user:{uid}")    # idempotent replay
        self.assertEqual(str(dep2["id"]), str(dep["id"]))
        self.assertEqual(ledger_ops.spendable(db, svc, uid), 100_000_000)
        self.assertEqual(s.withdrawable_usdc(db, uid), 100_000_000)
        self.assertTrue(s.list_deposits(db, uid, 10, None))

        # subscription first-period charge: creator 97 % / platform 3 %
        st = {"slug": "x", "price_monthly_micro": 20_000_000, "in_house": False, "owner_user_id": creator}
        charged, tx = ledger_ops.charge_subscription_start(db, svc, user_id=uid, subscription_id=str(uuid.uuid4()),
                                                           strategy=st, actor=f"user:{uid}")
        self.assertEqual(charged, 20_000_000)
        self.assertEqual(ledger_ops.spendable(db, svc, uid), 80_000_000)
        self.assertEqual(-svc.ledger.balance(db, ledger_ops.creator_payable(creator)), 19_400_000)
        self.assertGreaterEqual(s.total_credited(db, ledger_ops.creator_payable(creator)), 19_400_000)
        hist = s.ledger_history(db, ledger_ops.fee_balance(uid), 10, None)
        self.assertEqual([h["kind"] for h in hist][:2], ["subscription_start", "deposit"])

        # withdrawal: hold → two approvals (maker ≠ checker) → sent
        w = s.insert_withdrawal(db, user_id=uid, amount_micro=30_000_000, to_address=_addr(self.seed * 10 + 1))
        wid = str(w["id"])
        ledger_ops.hold_withdrawal(db, svc, user_id=uid, withdrawal_id=wid, amount=30_000_000, actor=f"user:{uid}")
        self.assertEqual(ledger_ops.spendable(db, svc, uid), 50_000_000)
        self.assertEqual(s.pending_withdrawals_total(db, uid), 30_000_000)
        # REVIEW_AUTH_API F4: the 20 spent on the subscription is no longer withdrawable (was 70: spending ignored)
        self.assertEqual(s.withdrawable_usdc(db, uid), 50_000_000)
        admin1, admin2 = str(self.a["id"]), str(self.c["id"])
        self.assertEqual(s.payout_approve_1(db, "withdrawal", wid, admin1, self.now), 1)
        self.assertEqual(s.payout_approve_2(db, "withdrawal", wid, admin1, self.now), 0)   # same admin refused
        self.assertEqual(s.payout_approve_2(db, "withdrawal", wid, admin2, self.now), 1)
        row = s.get_payout(db, "withdrawal", wid)
        tx_hash = "0x" + uuid.uuid4().hex + uuid.uuid4().hex
        sent_tx = ledger_ops.settle_sent(db, svc, kind="withdrawal", row=row, tx_hash=tx_hash, actor=f"admin:{admin2}")
        self.assertEqual(s.payout_mark_sent(db, "withdrawal", wid, tx_hash, sent_tx), 1)
        self.assertTrue(s.tx_hash_used(db, tx_hash))
        self.assertEqual(s.get_payout(db, "withdrawal", wid, for_update=False)["status"], "sent")
        self.assertTrue(s.admin_list_payouts(db, kind="withdrawal", status="sent", limit=10, cursor=None))

        # a rejected withdrawal releases its hold
        w2 = s.insert_withdrawal(db, user_id=uid, amount_micro=10_000_000, to_address=_addr(self.seed * 10 + 1))
        ledger_ops.hold_withdrawal(db, svc, user_id=uid, withdrawal_id=str(w2["id"]), amount=10_000_000,
                                   actor=f"user:{uid}")
        ledger_ops.release_hold(db, svc, kind="withdrawal", row=s.get_payout(db, "withdrawal", str(w2["id"])),
                                source_account=ledger_ops.fee_balance(uid), actor=f"admin:{admin1}")
        self.assertEqual(s.payout_reject(db, "withdrawal", str(w2["id"]), admin1, "not needed"), 1)
        self.assertEqual(ledger_ops.spendable(db, svc, uid), 50_000_000)

        # creator payout from the payable
        pay_code = ledger_ops.creator_payable(creator)
        acct = s.account_id(db, pay_code)
        self.assertEqual(s.account_code_by_id(db, acct), pay_code)
        p = s.insert_payout(db, user_id=creator, ledger_account_id=acct, amount_micro=10_000_000,
                            to_address=_addr(self.seed * 10 + 9))
        ledger_ops.hold_payout(db, svc, source_account=pay_code, payout_id=str(p["id"]), amount=10_000_000,
                               actor=f"user:{creator}")
        self.assertEqual(s.pending_payouts_total(db, creator), 10_000_000)
        self.assertEqual(len(s.list_user_payouts(db, creator, 10, None)), 1)

        # paid post purchase: creator price − $1, platform $1
        post = s.insert_post(db, creator_id=creator, strategy_id=None, title="Weekly notes", body="b",
                             price_micro=5_000_000, now=self.now)
        prow = {**s.get_post(db, str(post["id"])), "strategy_in_house": None}
        price, ptx = ledger_ops.charge_post(db, svc, user_id=uid, post_row=prow, actor=f"user:{uid}")
        self.assertTrue(s.insert_purchase(db, post_id=str(post["id"]), user_id=uid, price_micro=price, tx_id=ptx))
        self.assertFalse(s.insert_purchase(db, post_id=str(post["id"]), user_id=uid, price_micro=price, tx_id=ptx))
        self.assertTrue(s.has_purchased(db, str(post["id"]), uid))
        self.assertTrue(s.list_public_posts(db, strategy_slug=None, limit=5, cursor=None))
        # overspending is refused by the ledger
        from app.errors import InsufficientBalance
        with self.assertRaises(InsufficientBalance):
            ledger_ops.charge_plan(db, svc, user_id=str(self.c["id"]), plan="max", period_start=self.now,
                                   actor="user:c")
        s.mark_deposit_reversed(db, ref)

    # ------------------------------------------------------------------------------------------------ 0008 + creator
    def test_devices_mfa_alert_dedup_and_kyc_states(self) -> None:
        """user_devices / users.mfa_factor_hash (0008), alert dedup keys, KYC provider_approved (single admin)."""
        s, db = self.store, self.db
        uid = str(self.c["id"])
        h1, h2 = "a" * 64, "b" * 64
        self.assertFalse(s.record_device(db, uid, h1, "Chrome on macOS"))     # first device: silent
        self.assertFalse(s.record_device(db, uid, h1, "Chrome on macOS"))
        self.assertTrue(s.record_device(db, uid, h2, "Safari on iOS"))        # new device → alert
        self.assertFalse(s.record_device(db, uid, h2, None))
        self.assertIsNone(s.get_user(db, uid)["mfa_factor_hash"])
        s.set_mfa_factor_hash(db, uid, "c" * 64)
        self.assertEqual(s.get_user_by_firebase_uid(db, self.c["firebase_uid"])["mfa_factor_hash"], "c" * 64)
        with self.assertRaises(DbError):
            s.set_mfa_factor_hash(db, uid, "not-a-hash")                      # CHECK: hashes only
        key = f"builder_approval_missing:{uid}:{self.tag}"
        for _ in range(2):
            s.insert_alert(db, user_id=uid, severity="critical", kind="builder_approval_missing",
                           payload={"where": "subscribe"}, dedup_key=key)
        self.assertEqual(len(db.fetchall("SELECT id FROM alerts WHERE dedup_key = :k", {"k": key})), 1)
        # KYC: a provider GREEN is stored as provider_approved; a new session never replaces that applicant
        s.upsert_kyc_pending(db, user_id=uid, provider="sumsub", provider_ref="app-" + self.tag)
        self.assertEqual(s.set_kyc_status(db, uid, "provider_approved"), 1)
        s.upsert_kyc_pending(db, user_id=uid, provider="sumsub", provider_ref="other-" + self.tag)
        self.assertEqual(s.get_kyc(db, uid), {"provider": "sumsub", "provider_ref": "app-" + self.tag,
                                              "status": "provider_approved"})
        self.assertEqual(s.set_kyc_status(db, uid, "approved"), 1)

    def test_creator_posts_and_earnings_by_strategy(self) -> None:
        from app.api import ledger_ops
        from app.api.creator_earnings import earnings_breakdown
        from app.ledger import service

        s, db, svc = self.store, self.db, self.svc
        su = ApiRoleRunner(DB_URL, "postgres")
        t = uuid.uuid4().hex[:8]
        creator = str(s.create_user(db, firebase_uid=f"fbK{t}", email=f"k{t}@x.io", display_name="K",
                                    referral_code=f"K{t}", referred_by=None, mfa_enrolled=True)["id"])
        buyer = str(s.create_user(db, firebase_uid=f"fbQ{t}", email=f"q{t}@x.io", display_name="Q",
                                  referral_code=f"Q{t}", referred_by=None, mfa_enrolled=True)["id"])
        st = s.insert_strategy(db, owner_user_id=creator, slug=f"k-{t}", name="K strat", description=None,
                               markets=["BTC"], timeframe="1d", price_monthly_micro=20_000_000, profit_share_bps=1000)
        st2 = s.insert_strategy(db, owner_user_id=creator, slug=f"k2-{t}", name="K idle", description=None,
                                markets=["BTC"], timeframe="1d", price_monthly_micro=0, profit_share_bps=0)
        sid = str(st["id"])
        ver = s.insert_version(db, strategy_id=sid, version=1, code_hash="d" * 64, code_ciphertext=b"\x00c",
                               params={}, markets=["BTC"], timeframe="1d", lookback=300, max_leverage=2, backtest={})
        addr = _addr(self.seed * 10 + 77)
        sub = str(s.insert_subscription(db, user_id=buyer, strategy_id=sid, version_id=str(ver["id"]),
                                        trading_address=addr, master_address=addr, allocation_micro=500_000_000,
                                        max_leverage_x100=100, status="active",
                                        current_period_end=self.now + timedelta(days=30))["id"])
        service.post_transaction(su, f"test-topup:{t}", "deposit", "t",
                                 [("treasury:hl_usdc", 100_000_000), (ledger_ops.fee_balance(buyer), -100_000_000)], "t")
        stg = {"slug": st["slug"], "price_monthly_micro": 20_000_000, "in_house": False, "owner_user_id": creator}
        ledger_ops.charge_subscription_start(db, svc, user_id=buyer, subscription_id=sub, strategy=stg, actor="t")
        service.post_transaction(su, f"ps:{sub}:2026-10-01", "profit_share", "t",
                                 [(ledger_ops.fee_balance(buyer), 1_150_000), (ledger_ops.creator_payable(creator), -1_000_000),
                                  ("platform:revenue:profit_share", -150_000)], "system:settlement")
        bf = service.post_transaction(su, f"bf:{addr}:{self.seed}", "builder_fee", "t",
                                      [("builder:hl_receivable", 1000), (ledger_ops.creator_payable(creator), -500),
                                       ("platform:revenue:builder", -500)], "system:settlement")
        su.fetchall("""INSERT INTO fills (subscription_id, trading_address, coin, tid, px, sz, side, closed_pnl_micro,
                                          fee_micro, builder_fee_micro, cloid, time, builder_fee_recognised_at,
                                          builder_fee_ledger_tx_id)
                       VALUES (CAST(:s AS uuid), :a, 'BTC', :tid, 100, 1, 'buy', 0, 1000, 1000, NULL, now(), now(),
                               CAST(:tx AS uuid))""", {"s": sub, "a": addr, "tid": self.seed % 10**9, "tx": str(bf.id)})
        p1 = s.insert_post(db, creator_id=creator, strategy_id=sid, title="Strategy notes", body="body one",
                           price_micro=5_000_000, now=self.now)
        p2 = s.insert_post(db, creator_id=creator, strategy_id=None, title="General notes", body="body two",
                           price_micro=3_000_000, now=self.now)
        for p in (p1, p2):
            prow = {**s.get_post(db, str(p["id"])), "strategy_in_house": None}
            price, ptx = ledger_ops.charge_post(db, svc, user_id=buyer, post_row=prow, actor="t")
            s.insert_purchase(db, post_id=str(p["id"]), user_id=buyer, price_micro=price, tx_id=ptx)

        posts = s.list_creator_posts(db, creator, 1, None)
        self.assertEqual(len(posts), 2)                                   # limit + 1 (next page exists)
        allp = {r["title"]: r for r in s.list_creator_posts(db, creator, 10, None)}
        self.assertEqual((allp["Strategy notes"]["strategy_slug"], allp["Strategy notes"]["body"],
                          allp["Strategy notes"]["sales"], allp["Strategy notes"]["gross_sales_micro"]),
                         (st["slug"], "body one", 1, 5_000_000))
        self.assertIsNone(allp["General notes"]["strategy_slug"])
        self.assertEqual(s.list_creator_posts(db, buyer, 10, None), [])

        credits = s.creator_earnings_by_strategy(db, creator)
        rows, general, other = earnings_breakdown(s.active_subscribers_by_strategy(db, creator), credits)
        by = {str(r["strategy_id"]): r for r in rows}
        r = by[sid]
        self.assertEqual((r["subscription_share_micro"], r["profit_share_micro"], r["builder_share_micro"],
                          r["posts_micro"]), (19_400_000, 1_000_000, 500, 4_000_000))
        self.assertEqual(r["earned_micro"], 19_400_000 + 1_000_000 + 500 + 4_000_000)
        self.assertEqual(r["active_subscribers"], 1)
        self.assertEqual(by[str(st2["id"])]["earned_micro"], 0)
        self.assertEqual((general, other), (2_000_000, 0))
        self.assertEqual(sum(x["earned_micro"] for x in rows) + general + other,
                         s.total_credited(db, ledger_ops.creator_payable(creator)))

    # ------------------------------------------------------------------------------------------------ ops / admin
    def test_alerts_flags_changes_audit_idempotency(self) -> None:
        s, db = self.store, self.db
        uid, a1, a2 = str(self.b["id"]), str(self.a["id"]), str(self.c["id"])
        s.insert_alert(db, user_id=uid, severity="info", kind="t", payload={"x": 1})
        s.insert_alert(db, user_id=None, severity="critical", kind="ops_t", payload={})
        al = s.list_user_alerts(db, uid, 10, None)
        self.assertEqual(s.ack_user_alert(db, str(al[0]["id"]), uid, self.now), 1)
        ops = s.admin_list_alerts(db, severity="critical", unacked_only=True, ops_only=True, limit=10, cursor=None)
        self.assertEqual(s.admin_ack_alert(db, str(ops[0]["id"]), a1, self.now), 1)

        key = f"kill_switch_market:xyz:T{self.tag}"
        s.set_flag(db, key, True, f"admin:{a1}")
        self.assertTrue(s.propose_flag(db, key, False, f"admin:{a1}", self.now))
        self.assertFalse(s.propose_flag(db, key, False, f"admin:{a2}", self.now))
        self.assertEqual(s.get_flag(db, key, for_update=True)["pending_by"], f"admin:{a1}")
        self.assertEqual(s.clear_flag_proposal(db, key), 1)
        self.assertFalse(s.propose_flag(db, "kill_switch_market:NOPE" + self.tag, False, "admin:x", self.now))
        self.assertTrue(any(f["key"] == key for f in s.list_flags(db)))

        target = f"user:{uid}"
        ch = s.insert_change(db, kind="user_unsuspend", target=target, payload={}, reason="appeal ok", maker=a1)
        self.assertIsNotNone(s.open_change_for(db, "user_unsuspend", target))
        self.assertEqual(s.get_change(db, str(ch["id"]), for_update=True)["status"], "pending")
        with self.assertRaises(DbError):   # one pending change per (kind, target)
            s.insert_change(db, kind="user_unsuspend", target=target, payload={}, reason="again!", maker=a2)
        # checker must differ from maker (store filter AND table CHECK)
        self.assertIsNone(s.decide_change(db, str(ch["id"]), status="approved", checker=a1, now=self.now,
                                          decision_reason="self ok"))
        with self.assertRaises(DbError):
            db.fetchall("UPDATE admin_changes SET status = 'approved', checker_admin = maker_admin, decided_at = now() "
                        "WHERE id = CAST(:id AS uuid)", {"id": str(ch["id"])})
        done = s.decide_change(db, str(ch["id"]), status="approved", checker=a2, now=self.now, decision_reason="verified")
        self.assertEqual(done["status"], "approved")
        self.assertIsNone(s.decide_change(db, str(ch["id"]), status="rejected", checker=a2, now=self.now,
                                          decision_reason="changed"))
        with self.assertRaises(DbError) as cm:  # decided rows are immutable (trigger)
            db.fetchall("UPDATE admin_changes SET status = 'rejected' WHERE id = CAST(:id AS uuid)", {"id": str(ch["id"])})
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        self.assertTrue(s.list_changes(db, "approved", 10, None))
        self.assertEqual(s.set_user_status(db, uid, "active"), 1)

        s.insert_audit(db, actor=f"user:{uid}", action="test.event", target="", payload={"n": 1}, ip_hash=None)
        self.assertEqual(db.fetchall("SELECT * FROM verify_chain()"), [])

        idem = "k-" + uuid.uuid4().hex
        self.assertIsNone(s.idem_claim(db, user_id=uid, key=idem, scope="POST /x", fingerprint="f" * 64))
        s.idem_complete(db, user_id=uid, key=idem, status_code=201, response={"ok": True})
        again = s.idem_claim(db, user_id=uid, key=idem, scope="POST /x", fingerprint="f" * 64)
        self.assertEqual((again["status_code"], again["response"]), (201, {"ok": True}))
        with self.assertRaises(DbError):   # a stored response is final
            s.idem_complete(db, user_id=uid, key=idem, status_code=200, response={"ok": False})


if __name__ == "__main__":
    unittest.main()
