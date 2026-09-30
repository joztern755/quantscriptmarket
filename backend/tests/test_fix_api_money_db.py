"""Security-fix round, money / billing side, against a REAL migrated Postgres (0001–0012) — every API statement runs
AS app_api (ApiRoleRunner from test_api_store_db). Each test reproduces the finding, then shows it blocked.

  F4/H4  card-funded balance spent first; withdrawable = USDC-funded unspent; card-funded creator earnings held
  F3/H3  pause → unpause restores the billing state (no fresh `active`, no new grace), renewal due while paused
  H5     delisting ends subscriptions (closing / cancelled), billing stops, users alerted
  M6/L1  chargeback → reduce_only now; deposit marked reversed; a redelivered success does not flip it back
  M8     terms pinned at subscribe (trigger), immutable
  M1     withdrawal keeps accrued profit share + reserve
  M2/L6  paid-activity rule, flagged referees, suspended referrers (PgReferralLookup / tier stats)
  F9/M4  typed data issued once, no reject for 72 h, one tx hash settles one row

Run: AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/<migrated db> python3 -m unittest \
     tests.test_fix_api_money_db   (from backend/)
"""
from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

from test_api_store_db import DB_URL, RUN, _addr  # noqa: E402

if RUN:
    import re

    from test_api_store_db import ApiRoleRunner as _Runner  # noqa: E402

    from app.db.engine import DbError  # noqa: E402

    _TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+-]\d{2}:\d{2}$")

    class ApiRoleRunner(_Runner):
        """psql returns timestamps as JSON strings; the API code (like SQLAlchemy in prod) expects datetimes."""

        def fetchall(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            rows = super().fetchall(sql, params)
            return [{k: (datetime.fromisoformat(v) if isinstance(v, str) and _TS.match(v) else v)
                     for k, v in r.items()} if isinstance(r, dict) else r for r in rows]

UTC = timezone.utc
USD = 1_000_000


def _hash() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex


class Svc:
    """What app.api.billing_ops / ledger_ops need: the real store and ledger service, a recording notifier."""

    def __init__(self, store: Any, now: datetime) -> None:
        from app.api import ledger_ops
        from app.config import get_settings
        from app.ledger import service

        class Ledger:
            def ensure_account(self, conn: Any, code: str) -> None:
                kind, nn, owner = ledger_ops.account_spec(code)
                service.ensure_account(conn, code, kind, owner, non_negative=nn)

            def post(self, conn: Any, *, idempotency_key: str, kind: str, memo: str, entries: list,
                     created_by: str) -> str:
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
        self.notes: list[dict] = []
        self.notifier = SimpleNamespace(notify=lambda conn, **kw: self.notes.append(kw))
        self.settings = get_settings()
        self._now = now

    def now(self) -> datetime:
        return self._now


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (migrated through 0011) and psql")
class FixMoneyDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.api.store import SqlStore
        cls.db = ApiRoleRunner(DB_URL)
        cls.su = ApiRoleRunner(DB_URL, "postgres")
        cls.ex = ApiRoleRunner(DB_URL, "app_executor")
        cls.store = SqlStore()
        cls.now = datetime.now(UTC)
        cls.svc = Svc(cls.store, cls.now)

    # ------------------------------------------------------------------------------------------------ helpers
    def user(self, tag: str = "") -> str:
        t = uuid.uuid4().hex[:10]
        return str(self.store.create_user(self.db, firebase_uid=f"fx{tag}{t}", email=f"{tag}{t}@x.io",
                                          display_name="U", referral_code=f"R{t}", referred_by=None,
                                          mfa_enrolled=True)["id"])

    def card(self, uid: str, amount: int) -> str:
        from app.api import ledger_ops
        from app.payments.instructions import CreditInstruction
        pi = "pi_" + uuid.uuid4().hex
        self.store.insert_pending_deposit(self.db, user_id=uid, method="stripe", external_ref=pi, amount_micro=amount)
        ledger_ops.apply_credit(self.db, self.svc, CreditInstruction(
            user_id=uid, amount_micro=amount, external_ref=pi, idempotency_key=f"stripe:{pi}", method="stripe",
            debit_account="stripe:clearing", credit_account=ledger_ops.fee_balance(uid), withdrawable=False),
                                actor="stripe:webhook")
        return pi

    def usdc(self, uid: str, amount: int) -> str:
        from app.api import ledger_ops
        from app.payments.instructions import CreditInstruction
        h = _hash()
        ledger_ops.apply_credit(self.db, self.svc, CreditInstruction(
            user_id=uid, amount_micro=amount, external_ref=h, idempotency_key=f"usdc_hl:{h}", method="usdc_hl",
            debit_account="treasury:hl_usdc", credit_account=ledger_ops.fee_balance(uid), withdrawable=True),
                                actor=f"user:{uid}")
        return h

    def post_of(self, creator: str, price: int) -> dict:
        p = self.store.insert_post(self.db, creator_id=creator, strategy_id=None, title="Notes", body="b",
                                   price_micro=price, now=self.now)
        return {**self.store.get_post(self.db, str(p["id"])), "strategy_in_house": None}

    def buy(self, buyer: str, post: dict) -> str:
        from app.api import ledger_ops
        _, tx = ledger_ops.charge_post(self.db, self.svc, user_id=buyer, post_row=post, actor=f"user:{buyer}")
        return tx

    def strategy(self, owner: str, *, price: int = 20 * USD, ps: int = 1000, status: str = "listed") -> tuple[str, str]:
        s = self.store
        st = s.insert_strategy(self.db, owner_user_id=owner, slug=f"fx-{uuid.uuid4().hex[:10]}", name="Fx",
                               description=None, markets=["BTC"], timeframe="1d", price_monthly_micro=price,
                               profit_share_bps=ps)
        sid = str(st["id"])
        ver = s.insert_version(self.db, strategy_id=sid, version=1, code_hash="e" * 64, code_ciphertext=b"\x00x",
                               params={}, markets=["BTC"], timeframe="1d", lookback=300, max_leverage=2, backtest={})
        s.publish_version(self.db, str(ver["id"]), self.now)
        s.set_strategy_status(self.db, sid, status)
        return sid, str(ver["id"])

    def sub(self, uid: str, sid: str, vid: str, **cols: Any) -> str:
        addr = _addr(int(uuid.uuid4().hex[:12], 16))
        row = self.store.insert_subscription(self.db, user_id=uid, strategy_id=sid, version_id=vid,
                                             trading_address=addr, master_address=addr, allocation_micro=100 * USD,
                                             max_leverage_x100=100, status="active",
                                             current_period_end=self.now + timedelta(days=20))
        if cols:
            sets = ", ".join(f"{k} = :{k}" for k in cols)
            self.su.fetchall(f"UPDATE subscriptions SET {sets} WHERE id = CAST(:id AS uuid)", {**cols, "id": str(row["id"])})
        return str(row["id"])

    def sub_row(self, sub_id: str) -> dict:
        return self.su.fetchall("""SELECT status::text AS status, past_due_since, current_period_end,
                                          pre_pause_status::text AS pre_pause_status, cancel_positions::text AS cp,
                                          end_reason, cancelled_at, price_monthly_micro, profit_share_bps
                                     FROM subscriptions WHERE id = CAST(:id AS uuid)""", {"id": sub_id})[0]

    def spendable(self, uid: str) -> int:
        from app.api import ledger_ops
        return ledger_ops.spendable(self.db, self.svc, uid)

    # ------------------------------------------------------------------------------------------------ F4 / H4
    def test_card_lot_is_spent_first_and_never_withdrawable(self) -> None:
        s, db = self.store, self.db
        u, creator = self.user("u"), self.user("c")
        self.usdc(u, 1000 * USD)
        self.assertEqual(s.withdrawable_usdc(db, u), 1000 * USD)
        self.buy(u, self.post_of(creator, 400 * USD))            # USDC money spent on a (colluding) creator
        self.buy(u, self.post_of(creator, 400 * USD))
        self.buy(u, self.post_of(creator, 200 * USD))
        self.card(u, 1000 * USD)                                 # then a (stolen) card top-up
        # the review's exploit: Σ USDC credits − withdrawals ignored spending → 1000 withdrawable
        self.assertEqual(s.usdc_credited_minus_withdrawn(db, u), 1000 * USD)
        self.assertEqual(s.card_unspent(db, u), 1000 * USD)
        self.assertEqual(s.withdrawable_usdc(db, u), 0)          # blocked: the balance is card money
        self.usdc(u, 500 * USD)
        self.assertEqual(s.withdrawable_usdc(db, u), 500 * USD)
        self.buy(u, self.post_of(creator, 300 * USD))            # spends the CARD lot first
        self.assertEqual(s.card_unspent(db, u), 700 * USD)
        self.assertEqual(s.withdrawable_usdc(db, u), 500 * USD)

    def test_card_money_repaying_a_debt_is_not_a_card_lot(self) -> None:
        from app.api import ledger_ops
        from app.ledger import service
        s, db = self.store, self.db
        u = self.user("d")
        service.post_transaction(self.ex, f"ps:{uuid.uuid4()}:2026-10-01", "profit_share", "t",
                                 [(ledger_ops.fee_balance(u), 100 * USD), (f"ps_pending:{u}:platform", -100 * USD)],  # uncollected (C1, 0010)
                                 "system:settlement")
        self.assertEqual(self.spendable(u), -100 * USD)
        self.card(u, 150 * USD)                                   # 100 of it pays the debt at once
        self.assertEqual(s.card_unspent(db, u), 50 * USD)
        self.assertEqual(s.withdrawable_usdc(db, u), 0)
        self.usdc(u, 20 * USD)
        self.assertEqual(s.withdrawable_usdc(db, u), 20 * USD)

    def test_creator_earnings_from_card_spending_are_held(self) -> None:
        from app.api import billing_ops, ledger_ops
        s, db = self.store, self.db
        creator, card_buyer, usdc_buyer = self.user("k"), self.user("b"), self.user("b")
        code = ledger_ops.creator_payable(creator)
        self.card(card_buyer, 100 * USD)
        self.buy(card_buyer, self.post_of(creator, 50 * USD))    # creator gets 49 — funded by the card
        av = billing_ops.payout_available(db, self.svc, code)
        self.assertEqual((av["balance_micro"], av["held_micro"], av["available_micro"]), (49 * USD, 49 * USD, 0))
        self.usdc(usdc_buyer, 100 * USD)
        self.buy(usdc_buyer, self.post_of(creator, 30 * USD))    # +29, USDC-funded → available
        av = billing_ops.payout_available(db, self.svc, code)
        self.assertEqual((av["balance_micro"], av["held_micro"], av["available_micro"]), (78 * USD, 49 * USD, 29 * USD))
        # after the dispute window the card-funded part is released
        self.assertEqual(s.payable_card_held(db, code, self.now + timedelta(minutes=5)), 0)
        # mixed funding: 20 card + 30 USDC spent on one 50 post → 49 × 20/50 held (rounded up)
        mixed = self.user("m")
        self.usdc(mixed, 30 * USD)
        self.card(mixed, 20 * USD)
        self.buy(mixed, self.post_of(creator, 50 * USD))
        self.assertEqual(s.payable_card_held(db, code, self.now - timedelta(days=1)), 49 * USD + 19_600_000)

    # ------------------------------------------------------------------------------------------------ F3 / H3
    def test_unpause_restores_billing_state_and_charges_a_due_renewal(self) -> None:
        from app.api import billing_ops
        from app.errors import InsufficientBalance
        s, db, now = self.store, self.db, self.now
        owner, u = self.user("o"), self.user("p")
        sid, vid = self.strategy(owner, price=20 * USD)
        period_end = now - timedelta(days=4)
        sub = self.sub(u, sid, vid, status="reduce_only", past_due_since=(now - timedelta(days=4)).isoformat(),
                       current_period_end=period_end.isoformat())
        self.assertIsNotNone(s.pause_subscription(db, sub))
        self.assertEqual(self.sub_row(sub)["pre_pause_status"], "reduce_only")
        # before the fix: unpause set 'active' (free trading + a fresh 72 h grace). Now: the renewal is due → 402
        with self.assertRaises(InsufficientBalance):
            billing_ops.resume_subscription(db, self.svc, user_id=u, sub_id=sub, actor=f"user:{u}")
        self.assertEqual(self.sub_row(sub)["status"], "paused_user")
        self.usdc(u, 30 * USD)
        out = billing_ops.resume_subscription(db, self.svc, user_id=u, sub_id=sub, actor=f"user:{u}")
        self.assertEqual((out["status"], out["charged_micro"]), ("active", 20 * USD))
        row = self.sub_row(sub)
        self.assertEqual((row["status"], row["pre_pause_status"], row["past_due_since"]), ("active", None, None))
        self.assertGreater(row["current_period_end"], now)
        key = billing_ops.renewal_key(sub, period_end)
        n = self.su.fetchall("SELECT count(*) AS n FROM ledger_transactions WHERE idempotency_key = :k", {"k": key})
        self.assertEqual(n[0]["n"], 1)                            # the settlement job uses the same key
        self.assertEqual(self.spendable(u), 10 * USD)

    def test_unpause_keeps_past_due_clock(self) -> None:
        from app.api import billing_ops
        from app.ledger import service
        from app.api import ledger_ops
        s, db, now = self.store, self.db, self.now
        owner, u = self.user("o"), self.user("q")
        sid, vid = self.strategy(owner, price=0, ps=1000)
        service.post_transaction(self.ex, f"ps:{uuid.uuid4()}:2026-10-01", "profit_share", "t",
                                 [(ledger_ops.fee_balance(u), 5 * USD), (f"ps_pending:{u}:platform", -5 * USD)],  # uncollected (C1, 0010)
                                 "system:settlement")
        since = now - timedelta(hours=10)
        sub = self.sub(u, sid, vid, status="past_due", past_due_since=since.isoformat())
        s.pause_subscription(db, sub)
        out = billing_ops.resume_subscription(db, self.svc, user_id=u, sub_id=sub, actor=f"user:{u}")
        self.assertEqual(out["status"], "past_due")
        self.assertEqual(self.sub_row(sub)["past_due_since"], since)   # no fresh grace
        # the same cycle after the grace ran out → reduce_only, not active
        sub2 = self.sub(u, *self.strategy(owner, price=0, ps=1000), status="past_due",
                        past_due_since=(now - timedelta(hours=80)).isoformat())
        s.pause_subscription(db, sub2)
        self.assertEqual(billing_ops.resume_subscription(db, self.svc, user_id=u, sub_id=sub2,
                                                         actor=f"user:{u}")["status"], "reduce_only")

    def test_unpause_refused_when_strategy_not_listed(self) -> None:
        from app.api import billing_ops
        from app.errors import Conflict
        owner, u = self.user("o"), self.user("r")
        sid, vid = self.strategy(owner, price=0, ps=0)
        sub = self.sub(u, sid, vid)
        self.store.pause_subscription(self.db, sub)
        self.store.set_strategy_status(self.db, sid, "paused")
        with self.assertRaises(Conflict):
            billing_ops.resume_subscription(self.db, self.svc, user_id=u, sub_id=sub, actor=f"user:{u}")

    # ------------------------------------------------------------------------------------------------ H5
    def test_delisting_ends_subscriptions_and_billing(self) -> None:
        from app.api import billing_ops
        owner = self.user("o")
        sid, vid = self.strategy(owner, price=10 * USD)
        a, b, c = self.user("s"), self.user("s"), self.user("s")
        s_active = self.sub(a, sid, vid)
        s_ro = self.sub(b, sid, vid, status="reduce_only", past_due_since=self.now.isoformat())
        s_paused = self.sub(c, sid, vid)
        self.store.pause_subscription(self.db, s_paused)
        self.store.set_strategy_status(self.db, sid, "delisted")
        before = len(self.svc.notes)
        out = billing_ops.end_strategy_subscriptions(self.db, self.svc, strategy_id=sid, strategy_name="Fx")
        self.assertEqual(out, {"closing": 2, "cancelled": 1})
        for sub in (s_active, s_ro):
            r = self.sub_row(sub)
            self.assertEqual((r["status"], r["cp"], r["end_reason"]), ("closing", "close", "strategy_delisted"))
        r = self.sub_row(s_paused)
        self.assertEqual((r["status"], r["cp"], r["end_reason"]), ("cancelled", "leave", "strategy_delisted"))
        self.assertIsNotNone(r["cancelled_at"])
        notes = self.svc.notes[before:]
        self.assertEqual(sorted(n["user_id"] for n in notes), sorted([a, b, c]))
        self.assertTrue(all(n["kind"] == "strategy_ended" and n["severity"] == "critical" for n in notes))
        # the settlement job bills only active/past_due/reduce_only → 'closing' is never renewed nor re-activated
        from app.execution.settlement import BILLABLE
        self.assertNotIn("closing", BILLABLE)

    # ------------------------------------------------------------------------------------------------ M6 / L1
    def test_chargeback_restricts_trading_and_marks_deposit_reversed(self) -> None:
        from app.api import billing_ops, ledger_ops
        from app.payments.instructions import DebitInstruction
        s, db = self.store, self.db
        owner, u = self.user("o"), self.user("v")
        sid, vid = self.strategy(owner, price=0, ps=0)
        sub = self.sub(u, sid, vid)
        paused = self.sub(u, *self.strategy(owner, price=0, ps=0))
        s.pause_subscription(db, paused)
        pi = self.card(u, 50 * USD)
        self.buy(u, self.post_of(owner, 40 * USD))
        instr = DebitInstruction(user_id=u, amount_micro=50 * USD, external_ref="dp_" + uuid.uuid4().hex[:10],
                                 idempotency_key=f"stripe:dispute:dp_{uuid.uuid4().hex[:10]}", method="stripe",
                                 debit_account=ledger_ops.fee_balance(u), credit_account="stripe:clearing",
                                 kind="stripe_dispute", meta={"payment_intent": pi, "full_reversal": True})
        self.assertIsNotNone(ledger_ops.apply_debit(db, self.svc, instr, actor="stripe:webhook"))
        self.assertEqual(self.spendable(u), -40 * USD)
        self.assertEqual(self.sub_row(sub)["status"], "active")          # before: trading on until 00:30 + 72 h
        rows = billing_ops.after_payment_reversal(db, self.svc, instr)
        self.assertEqual([str(r["id"]) for r in rows], [sub])
        self.assertEqual(self.sub_row(sub)["status"], "reduce_only")
        self.assertEqual(self.sub_row(paused)["pre_pause_status"], "reduce_only")   # resumes restricted
        dep = self.su.fetchall("SELECT status::text AS s FROM deposits WHERE external_ref = :r", {"r": pi})
        self.assertEqual(dep, [{"s": "reversed"}])
        # a redelivered payment_intent.succeeded no longer flips the reversed row back to credited (L1)
        s.mark_deposit_credited(db, user_id=u, method="stripe", external_ref=pi, amount_micro=50 * USD,
                                tx_id=str(self.su.fetchall("SELECT id FROM ledger_transactions LIMIT 1")[0]["id"]),
                                withdrawable=False)
        dep = self.su.fetchall("SELECT status::text AS s FROM deposits WHERE external_ref = :r", {"r": pi})
        self.assertEqual(dep, [{"s": "reversed"}])

    # ------------------------------------------------------------------------------------------------ M8
    def test_terms_are_pinned_at_subscribe(self) -> None:
        owner, u = self.user("o"), self.user("t")
        sid, vid = self.strategy(owner, price=15 * USD, ps=800)
        sub = self.sub(u, sid, vid)                                 # the API passes nothing → trigger pins
        self.assertEqual((self.sub_row(sub)["price_monthly_micro"], self.sub_row(sub)["profit_share_bps"]),
                         (15 * USD, 800))
        self.store.set_strategy_price(self.db, sid, 50 * USD)       # a later (maker-checker) price change …
        self.assertEqual(self.sub_row(sub)["price_monthly_micro"], 15 * USD)   # … applies to new subscribers only
        with self.assertRaises(DbError) as cm:
            self.db.fetchall("UPDATE subscriptions SET price_monthly_micro = 1 WHERE id = CAST(:id AS uuid) RETURNING id",
                             {"id": sub})
        self.assertEqual(cm.exception.sqlstate, "AJ422")
        rows = self.su.fetchall("""SELECT coalesce(s.price_monthly_micro, st.price_monthly_micro, 0) AS p
                                     FROM subscriptions s JOIN strategies st ON st.id = s.strategy_id
                                    WHERE s.id = CAST(:id AS uuid)""", {"id": sub})
        self.assertEqual(rows[0]["p"], 15 * USD)                    # what the settlement repo bills

    # ------------------------------------------------------------------------------------------------ M1
    def test_withdrawal_headroom_keeps_accrued_profit_share(self) -> None:
        from app.api import billing_ops
        from app.errors import Conflict, InsufficientBalance
        owner, u = self.user("o"), self.user("w")
        sid, vid = self.strategy(owner, price=0, ps=1000)
        sub = self.sub(u, sid, vid)
        self.usdc(u, 2000 * USD)
        self.su.fetchall("""INSERT INTO fills (subscription_id, trading_address, coin, tid, px, sz, side,
                                               closed_pnl_micro, fee_micro, builder_fee_micro, time)
                            SELECT id, trading_address, 'BTC', :tid, 100, 1, 'sell', :pnl, 0, 0, now()
                              FROM subscriptions WHERE id = CAST(:id AS uuid)""",
                         {"tid": int(uuid.uuid4().hex[:12], 16), "pnl": 20_000 * USD, "id": sub})
        accrued = billing_ops.accrued_profit_share(self.db, self.svc, u)
        self.assertEqual(accrued, 20_000 * USD * 1150 // 10_000)    # 10 % creator + 1.5 % platform (on top)
        with self.assertRaises(InsufficientBalance):
            billing_ops.require_withdrawal_headroom(self.db, self.svc, user_id=u, amount_micro=2000 * USD)
        self.su.fetchall("UPDATE subscriptions SET status = 'past_due', past_due_since = now() WHERE id = CAST(:id AS uuid)",
                         {"id": sub})
        with self.assertRaises(Conflict):
            billing_ops.require_withdrawal_headroom(self.db, self.svc, user_id=u, amount_micro=1 * USD)

    # ------------------------------------------------------------------------------------------------ M2 / L6
    def test_referral_rewards_need_paid_activity_active_referrer_and_no_flag(self) -> None:
        from app.execution.pg import PgReferralLookup
        s, db = self.store, self.db

        class Db:
            def __init__(self, runner: Any) -> None:
                self.r = runner

            def one(self, sql: str, **p: Any) -> Any:
                rows = self.r.fetchall(sql, p)
                return rows[0] if rows else None

            def all(self, sql: str, **p: Any) -> Any:
                return self.r.fetchall(sql, p)

        look = PgReferralLookup(Db(self.ex))
        ref, u = self.user("ref"), self.user("ee")
        self.assertTrue(s.bind_referrer(db, user_id=u, referrer_id=ref))
        # free showcase-only usage (SILVER): no paid activity → no referral reward
        self.assertIsNone(look.referrer_share(u))
        owner = self.user("o")
        self.usdc(u, 20 * USD)
        self.buy(u, self.post_of(owner, 5 * USD))
        self.assertEqual(look.referrer_share(u)[0], ref)
        s.flag_referral(db, u, "self_referral_suspected:test:same_network", self.now)
        self.assertIsNone(look.referrer_share(u))                   # flagged → nothing until ops clears it
        self.su.fetchall("UPDATE users SET referral_flagged_at = NULL WHERE id = CAST(:u AS uuid)", {"u": u})
        s.set_user_status(db, ref, "suspended")
        self.assertIsNone(look.referrer_share(u))                   # L6: suspended referrers earn nothing
        stats = s.referral_stats(db, ref, self.now - timedelta(days=30))
        self.assertEqual(int(stats["total"]), 1)

    # ------------------------------------------------------------------------------------------------ L7
    def test_executor_blocks_entries_after_the_grace_period(self) -> None:
        from app.execution.pg import PgSubscriptionRepo

        class Db:
            def __init__(self, runner: Any) -> None:
                self.r = runner

            def one(self, sql: str, **p: Any) -> Any:
                rows = self.r.fetchall(sql, p)
                return rows[0] if rows else None

            def all(self, sql: str, **p: Any) -> Any:
                return self.r.fetchall(sql, p)

        owner, u = self.user("o"), self.user("g")
        self.su.fetchall("""INSERT INTO user_contacts (user_id, telegram_chat_id, telegram_linked_at, email,
                                                       email_verified_at)
                            VALUES (CAST(:u AS uuid), 42, now(), 'g@x.io', now())""", {"u": u})
        sid, vid = self.strategy(owner, price=0, ps=0)
        inside = self.sub(u, sid, vid, status="past_due", past_due_since=(self.now - timedelta(hours=10)).isoformat())
        u2 = self.user("g")
        self.su.fetchall("""INSERT INTO user_contacts (user_id, telegram_chat_id, telegram_linked_at, email,
                                                       email_verified_at)
                            VALUES (CAST(:u AS uuid), 43, now(), 'h@x.io', now())""", {"u": u2})
        expired = self.sub(u2, sid, vid, status="past_due", past_due_since=(self.now - timedelta(hours=80)).isoformat())
        repo = PgSubscriptionRepo(Db(self.ex), clock=lambda: self.now)
        self.assertTrue(repo.get_subscription(inside).entries_allowed)
        # before: past_due kept opening positions until the next 00:30 settlement (up to ~96 h)
        self.assertFalse(repo.get_subscription(expired).entries_allowed)

    # ------------------------------------------------------------------------------------------------ F9 / M4
    def test_payout_typed_data_once_no_reject_after_issue_and_unique_tx_hash(self) -> None:
        from app.api import ledger_ops
        s, db = self.store, self.db
        u, a1, a2 = self.user("w"), self.user("a"), self.user("a")
        self.usdc(u, 100 * USD)
        rows = []
        for _ in range(2):
            w = s.insert_withdrawal(db, user_id=u, amount_micro=10 * USD, to_address=_addr(7))
            ledger_ops.hold_withdrawal(db, self.svc, user_id=u, withdrawal_id=str(w["id"]), amount=10 * USD, actor="t")
            s.payout_approve_1(db, "withdrawal", str(w["id"]), a1, self.now)
            s.payout_approve_2(db, "withdrawal", str(w["id"]), a2, self.now)
            rows.append(str(w["id"]))
        wid, wid2 = rows
        first = s.set_payout_send(db, "withdrawal", wid, nonce=1_700_000_000_000, admin_id=a1, now=self.now)
        again = s.set_payout_send(db, "withdrawal", wid, nonce=1_800_000_000_000, admin_id=a2, now=self.now)
        self.assertEqual(int(first["send_nonce"]), 1_700_000_000_000)
        self.assertEqual(int(again["send_nonce"]), 1_700_000_000_000)          # same payload → one transfer
        with self.assertRaises(DbError) as cm:                                  # the signed send may still execute
            s.payout_reject(db, "withdrawal", wid, a1, "changed my mind")
        self.assertEqual(cm.exception.sqlstate, "AJ409")
        h = _hash()
        self.assertTrue(s.claim_payout_tx_hash(db, h, "withdrawal", wid))
        self.assertFalse(s.claim_payout_tx_hash(db, h, "withdrawal", wid2))     # one transfer, one payout
        self.assertEqual(s.payout_mark_sent(db, "withdrawal", wid, h, None), 1)
        with self.assertRaises(DbError) as cm:
            s.payout_mark_sent(db, "withdrawal", wid2, h.upper().replace("0X", "0x"), None)
        self.assertEqual(cm.exception.sqlstate, "23505")                         # unique (lower(tx_hash))
        # a payout whose typed data was issued > 72 h ago may be rejected (the signed nonce can no longer execute)
        s.set_payout_send(db, "withdrawal", wid2, nonce=1_600_000_000_000, admin_id=a1, now=self.now - timedelta(days=4))
        self.assertEqual(s.payout_reject(db, "withdrawal", wid2, a1, "expired payload"), 1)


if __name__ == "__main__":
    unittest.main()
