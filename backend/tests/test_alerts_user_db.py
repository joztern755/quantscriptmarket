"""User alerts against a REAL migrated Postgres 16 (0001–0007), with fake Telegram / email sinks.

API-side functions run AS app_api, the delivery worker AS app_executor (so the tests also prove the grants).
Runs when AIJALON_TEST_DATABASE_URL points at a database migrated through 0007 and `psql` is on PATH:
    createdb aj_alerts && python backend/scripts/migrate.py --database-url postgresql://postgres@localhost:55432/aj_alerts
    AIJALON_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/aj_alerts python -m unittest tests.test_alerts_user_db
Covers: Telegram linking (/start token single-use + expiry, /stop, blocked/unblocked), email confirmation (account
email, 6-digit code: attempts persisted, expiry, supersede), the mandatory-contacts gate + entries gate (24 h grace),
mute preferences (mandatory refused by API and DB), delivery routing (email policy, mutes, idempotency, retry with
backoff, 403 → lapsed link + email), events_outbox materialisation (+ derived trade_pnl, ops paging), ops fan-out
(kill switch → market_paused), low-balance alerts via ledger_ops.post, daily PnL summary, test alert.
"""
from __future__ import annotations

import re
import sys
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api_store_db import DB_URL, RUN  # noqa: E402  (same env var + psql check)

if RUN:
    from test_api_store_db import ApiRoleRunner  # noqa: E402

    from app.alerts import delivery, prefs, telegram_bot, user_sinks  # noqa: E402
    from app.alerts.notifier import TransientSinkError  # noqa: E402
    from app.alerts.user_sinks import TelegramBlocked  # noqa: E402
    from app.api.store import SqlStore  # noqa: E402
    from app.db.engine import DbError  # noqa: E402
    from app.errors import ValidationFailed  # noqa: E402

PEPPER = b"p" * 32
ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")


class RunnerDb:
    def __init__(self, runner: Any) -> None:
        self.runner = runner

    @contextmanager
    def begin(self):
        yield self.runner


class FakeTelegram:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail: dict[int, Exception] = {}

    def send_message(self, chat_id: int, text: str) -> None:
        err = self.fail.get(int(chat_id))
        if err is not None:
            raise err
        self.sent.append((int(chat_id), text))

    def to(self, chat: int) -> list[str]:
        return [t for c, t in self.sent if c == chat]


class FakeEmail:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail: dict[str, Exception] = {}

    def send(self, to: str, subject: str, text: str) -> None:
        err = self.fail.get(to)
        if err is not None:
            raise err
        self.sent.append((to, subject, text))

    def to(self, addr: str) -> list[tuple[str, str]]:
        return [(s, t) for a, s, t in self.sent if a == addr]


def _settings(**kw: Any) -> Any:
    base = dict(web_origin="https://aijalon.trade", telegram_ops_chat_id="-1001", ops_emails=("ops@aijalon.test",),
                telegram_bot_token="x", email_provider_api_key="y", email_from="alerts@aijalon.trade")
    base.update(kw)
    return SimpleNamespace(**base)


@unittest.skipUnless(RUN, "needs AIJALON_TEST_DATABASE_URL (migrated through 0007) and psql")
class UserAlertsDbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = ApiRoleRunner(DB_URL, "app_api")
        cls.exe = ApiRoleRunner(DB_URL, "app_executor")
        cls.su = ApiRoleRunner(DB_URL, "postgres")
        cls.store = SqlStore()
        chk = cls.su.fetchall("SELECT to_regclass('public.user_contacts') IS NOT NULL AS ok")
        if not chk[0]["ok"]:
            raise unittest.SkipTest("database not migrated through 0007")

    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.tag = uuid.uuid4().hex[:10]
        self.chat_seq = int(uuid.uuid4().int % 10**12) + 10**12

    # ------------------------------------------------------------------------------------------ helpers
    def user(self, email: str | None = None) -> str:
        t = uuid.uuid4().hex[:10]
        u = self.store.create_user(self.api, firebase_uid=f"fb{t}", email=email or f"u{t}@example.com",
                                   display_name="U", referral_code=f"R{t}", referred_by=None, mfa_enrolled=True)
        return str(u["id"])

    def chat(self) -> int:
        self.chat_seq += 1
        return self.chat_seq

    def link(self, uid: str, chat: int) -> None:
        link = telegram_bot.create_link(self.api, user_id=uid, now=self.now, bot_username="aijalon_bot")
        token = link["url"].split("start=", 1)[1]
        r = telegram_bot.handle_update(self.api, self.msg(chat, f"/start {token}"), self.now)
        self.assertIn("Linked", r["text"])

    def verify_email(self, uid: str, addr: str) -> None:
        mail = FakeEmail()
        user_sinks.start_email_verification(self.api, user_id=uid, email=addr, now=self.now, pepper=PEPPER,
                                            provider=mail)
        code = re.search(r"code is (\d{6})", mail.sent[-1][2]).group(1)
        res = user_sinks.verify_email_code(self.api, user_id=uid, code=code, now=self.now, pepper=PEPPER)
        self.assertTrue(res.ok)

    def ready_user(self) -> tuple[str, int, str]:
        addr = f"a{uuid.uuid4().hex[:10]}@example.com"
        uid = self.user(addr)
        chat = self.chat()
        self.link(uid, chat)
        user_sinks.confirm_account_email(self.api, user_id=uid, claims_email=addr, claims_email_verified=True,
                                         now=self.now)
        return uid, chat, addr

    @staticmethod
    def msg(chat: int, text: str, ctype: str = "private") -> dict:
        return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": ctype}, "text": text}}

    def alert(self, uid: str | None, kind: str, severity: str = "info", payload: dict | None = None) -> str:
        r = self.api.fetchall("""INSERT INTO alerts (user_id, severity, kind, payload, created_at)
                                 VALUES (CAST(:u AS uuid), CAST(:s AS alert_severity), :k, CAST(:p AS jsonb),
                                         CAST(:now AS timestamptz)) RETURNING id""",
                              {"u": uid, "s": severity, "k": kind, "p": payload or {}, "now": self.now})
        return str(r[0]["id"])

    def deliveries(self, alert_id: str) -> dict[str, dict]:
        return {r["channel"]: r for r in self.su.fetchall(
            "SELECT channel, status, attempts, last_error, next_attempt_at FROM alert_deliveries WHERE alert_id = CAST(:a AS uuid)",
            {"a": alert_id})}

    def run_worker(self, tg: FakeTelegram, mail: FakeEmail, now: datetime | None = None, **kw: Any) -> dict:
        return delivery.deliver_outbox(RunnerDb(self.exe), now or self.now, settings=_settings(), telegram=tg,
                                       email=mail, **kw)

    def strategy_sub(self, uid: str, markets: list[str], price: int = 20_000_000) -> tuple[str, str, str]:
        s, db = self.store, self.api
        owner = self.user()
        st = s.insert_strategy(db, owner_user_id=owner, slug=f"t-{uuid.uuid4().hex[:10]}", name=f"Strat {self.tag}",
                               description="d", markets=markets, timeframe="1d", price_monthly_micro=price,
                               profit_share_bps=0)
        ver = s.insert_version(db, strategy_id=str(st["id"]), version=1, code_hash="c" * 64,
                               code_ciphertext=b"\x00x", params={}, markets=markets, timeframe="1d", lookback=300,
                               max_leverage=2, backtest={})
        addr = "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
        sub = s.insert_subscription(db, user_id=uid, strategy_id=str(st["id"]), version_id=str(ver["id"]),
                                    trading_address=addr, master_address=addr, allocation_micro=100_000_000,
                                    max_leverage_x100=100, status="active",
                                    current_period_end=self.now + timedelta(days=30))
        return str(st["id"]), str(ver["id"]), str(sub["id"])

    # ------------------------------------------------------------------------------------------ Telegram linking
    def test_telegram_link_flow(self) -> None:
        uid = self.user()
        chat = self.chat()
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertEqual((st.telegram, st.email_verified, st.ready, st.entries_allowed), ("unlinked", False, False, False))
        link = telegram_bot.create_link(self.api, user_id=uid, now=self.now, bot_username="@aijalon_bot")
        self.assertTrue(link["url"].startswith("https://t.me/aijalon_bot?start="))
        token = link["url"].split("start=", 1)[1]
        self.assertRegex(token, r"^[A-Za-z0-9_-]{32}$")
        stored = self.su.fetchall("SELECT token_hash FROM telegram_link_tokens WHERE user_id = CAST(:u AS uuid)", {"u": uid})
        self.assertEqual([r["token_hash"] for r in stored], [telegram_bot.token_hash(token)])   # hash only
        # group chats cannot link
        r = telegram_bot.handle_update(self.api, self.msg(-chat, f"/start {token}", "group"), self.now)
        self.assertIn("private", r["text"])
        self.assertEqual(user_sinks.contact_status(self.api, uid, self.now).telegram, "unlinked")
        # expired token (a link created 11 minutes ago)
        old = telegram_bot.create_link(self.api, user_id=uid, now=self.now - timedelta(minutes=11), bot_username="aijalon_bot")
        r = telegram_bot.handle_update(self.api, self.msg(chat, "/start " + old["url"].split("start=")[1]), self.now)
        self.assertIn("expired", r["text"])
        # valid token → linked; reply goes back in the webhook response
        r = telegram_bot.handle_update(self.api, self.msg(chat, f"/start {token}"), self.now)
        self.assertEqual((r["method"], r["chat_id"]), ("sendMessage", chat))
        self.assertIn("Linked", r["text"])
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertEqual(st.telegram, "linked")
        # single use
        r = telegram_bot.handle_update(self.api, self.msg(chat + 1, f"/start {token}"), self.now)
        self.assertIn("expired", r["text"])
        self.assertEqual(self.su.fetchall("SELECT telegram_chat_id FROM user_contacts WHERE user_id = CAST(:u AS uuid)",
                                          {"u": uid})[0]["telegram_chat_id"], chat)
        # a used token row is immutable (DB guard)
        with self.assertRaises(DbError) as cm:
            self.api.fetchall("UPDATE telegram_link_tokens SET used_chat_id = 1 WHERE token_hash = :h RETURNING token_hash",
                              {"h": telegram_bot.token_hash(token)})
        self.assertEqual(cm.exception.sqlstate, "AJ403")
        # /help while linked
        self.assertIn("/stop", telegram_bot.handle_update(self.api, self.msg(chat, "/help"), self.now)["text"])

        # email → contacts ready → entries allowed
        user_sinks.confirm_account_email(self.api, user_id=uid, claims_email=st.account_email,
                                         claims_email_verified=True, now=self.now)
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertTrue(st.ready and st.entries_allowed)

        # /stop → stopped, mandatory email alert queued, 24 h grace for new entries
        r = telegram_bot.handle_update(self.api, self.msg(chat, "/stop"), self.now)
        self.assertIn("Unlinked", r["text"])
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertEqual(st.telegram, "stopped")
        self.assertFalse(st.ready)
        self.assertTrue(st.entries_allowed)
        self.assertEqual(st.entries_pause_at, self.now + timedelta(hours=24))
        self.assertTrue(user_sinks.entries_allowed(self.exe, uid, self.now + timedelta(hours=23)))
        self.assertFalse(user_sinks.entries_allowed(self.exe, uid, self.now + timedelta(hours=25)))
        kinds = [r["kind"] for r in self.su.fetchall("SELECT kind FROM alerts WHERE user_id = CAST(:u AS uuid)", {"u": uid})]
        self.assertIn("telegram_unreachable", kinds)
        # tokenless /start after /stop does NOT re-link (a new one-time link is needed)
        r = telegram_bot.handle_update(self.api, self.msg(chat, "/start"), self.now)
        self.assertIn("Link Telegram", r["text"])
        self.assertEqual(user_sinks.contact_status(self.api, uid, self.now).telegram, "stopped")
        self.assertIn("not linked", telegram_bot.handle_update(self.api, self.msg(chat, "/stop"), self.now)["text"])

    def test_bot_blocked_and_unblocked_via_updates(self) -> None:
        uid, chat, _ = self.ready_user()
        upd = {"update_id": 2, "my_chat_member": {"chat": {"id": chat, "type": "private"},
                                                  "new_chat_member": {"status": "kicked"}}}
        self.assertIsNone(telegram_bot.handle_update(self.api, upd, self.now))
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertEqual(st.telegram, "blocked")
        self.assertTrue(st.entries_allowed)     # grace
        upd["my_chat_member"]["new_chat_member"]["status"] = "member"
        telegram_bot.handle_update(self.api, upd, self.now)
        self.assertEqual(user_sinks.contact_status(self.api, uid, self.now).telegram, "linked")
        # tokenless /start from the same chat also re-activates a BLOCKED link
        user_sinks.mark_telegram_blocked(self.exe, user_id=uid, now=self.now, reason="blocked")
        r = telegram_bot.handle_update(self.api, self.msg(chat, "/start"), self.now)
        self.assertIn("Welcome back", r["text"])
        self.assertEqual(user_sinks.contact_status(self.api, uid, self.now).telegram, "linked")

    def test_link_rate_limit(self) -> None:
        from app.errors import RateLimited
        uid = self.user()
        for _ in range(telegram_bot.LINKS_PER_HOUR):
            telegram_bot.create_link(self.api, user_id=uid, now=self.now, bot_username="aijalon_bot")
        with self.assertRaises(RateLimited):
            telegram_bot.create_link(self.api, user_id=uid, now=self.now, bot_username="aijalon_bot")
        with self.assertRaises(user_sinks.AlertsUnavailable):
            telegram_bot.create_link(self.api, user_id=self.user(), now=self.now, bot_username="")

    # ------------------------------------------------------------------------------------------ email
    def test_email_code_flow(self) -> None:
        uid = self.user()
        from app.errors import Conflict
        with self.assertRaises(Conflict):      # provider did not verify the account email
            user_sinks.confirm_account_email(self.api, user_id=uid, claims_email="x@example.com",
                                             claims_email_verified=False, now=self.now)
        mail = FakeEmail()
        new = f"New.{self.tag}@Example.COM"
        exp = user_sinks.start_email_verification(self.api, user_id=uid, email=new, now=self.now, pepper=PEPPER,
                                                  provider=mail)
        self.assertEqual(exp, self.now + timedelta(minutes=10))
        to, subject, text = mail.sent[-1]
        self.assertEqual(to, f"New.{self.tag}@example.com")
        code = re.search(r"code is (\d{6})", text).group(1)
        row = self.su.fetchall("SELECT code_hash, attempts FROM email_verification_codes WHERE user_id = CAST(:u AS uuid)",
                               {"u": uid})[0]
        self.assertNotIn(code, row["code_hash"])
        self.assertEqual(user_sinks.contact_status(self.api, uid, self.now).pending_email, to)
        wrong = "000000" if code != "000000" else "111111"
        r = user_sinks.verify_email_code(self.api, user_id=uid, code=wrong, now=self.now, pepper=PEPPER)
        self.assertEqual((r.ok, r.reason, r.attempts_left), (False, "wrong_code", 4))
        # the attempt counter is committed even though the check failed
        self.assertEqual(self.su.fetchall("SELECT attempts FROM email_verification_codes WHERE user_id = CAST(:u AS uuid)",
                                          {"u": uid})[0]["attempts"], 1)
        # expired
        r = user_sinks.verify_email_code(self.api, user_id=uid, code=code, now=self.now + timedelta(minutes=11),
                                         pepper=PEPPER)
        self.assertEqual(r.reason, "expired")
        r = user_sinks.verify_email_code(self.api, user_id=uid, code=code, now=self.now, pepper=PEPPER)
        self.assertTrue(r.ok)
        self.assertEqual((r.email, r.previous_email), (to, None))
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertEqual((st.email, st.email_verified, st.pending_email), (to, True, None))
        self.assertEqual(user_sinks.verify_email_code(self.api, user_id=uid, code=code, now=self.now,
                                                      pepper=PEPPER).reason, "no_code")
        # change to another address → previous returned; 5 wrong attempts kill the code
        mail2 = FakeEmail()
        user_sinks.start_email_verification(self.api, user_id=uid, email=f"b{self.tag}@example.com", now=self.now,
                                            pepper=PEPPER, provider=mail2)
        code2 = re.search(r"code is (\d{6})", mail2.sent[-1][2]).group(1)
        bad = "000000" if code2 != "000000" else "111111"
        for i in range(5):
            r = user_sinks.verify_email_code(self.api, user_id=uid, code=bad, now=self.now, pepper=PEPPER)
        self.assertEqual(r.reason, "too_many_attempts")
        r = user_sinks.verify_email_code(self.api, user_id=uid, code=code2, now=self.now, pepper=PEPPER)
        self.assertEqual((r.ok, r.reason), (False, "too_many_attempts"))
        # a new code supersedes; success reports the previous address
        mail3 = FakeEmail()
        user_sinks.start_email_verification(self.api, user_id=uid, email=f"b{self.tag}@example.com", now=self.now,
                                            pepper=PEPPER, provider=mail3)
        code3 = re.search(r"code is (\d{6})", mail3.sent[-1][2]).group(1)
        r = user_sinks.verify_email_code(self.api, user_id=uid, code=code3, now=self.now, pepper=PEPPER)
        self.assertTrue(r.ok)
        self.assertEqual(r.previous_email, to)
        # rate limit: 5 codes per hour (3 sent so far)
        from app.errors import RateLimited
        user_sinks.start_email_verification(self.api, user_id=uid, email=f"c{self.tag}@example.com", now=self.now,
                                            pepper=PEPPER, provider=FakeEmail())
        user_sinks.start_email_verification(self.api, user_id=uid, email=f"c{self.tag}@example.com", now=self.now,
                                            pepper=PEPPER, provider=FakeEmail())
        with self.assertRaises(RateLimited):
            user_sinks.start_email_verification(self.api, user_id=uid, email=f"c{self.tag}@example.com",
                                                now=self.now, pepper=PEPPER, provider=FakeEmail())
        # one live code per user (DB)
        n = self.su.fetchall("""SELECT count(*) AS n FROM email_verification_codes WHERE user_id = CAST(:u AS uuid)
                                AND consumed_at IS NULL AND superseded_at IS NULL""", {"u": uid})[0]["n"]
        self.assertEqual(n, 1)

    def test_email_provider_failures(self) -> None:
        uid = self.user()
        with self.assertRaises(user_sinks.AlertsUnavailable):
            user_sinks.start_email_verification(self.api, user_id=uid, email="a@example.com", now=self.now,
                                                pepper=PEPPER, provider=None)
        mail = FakeEmail()
        mail.fail["a@example.com"] = TransientSinkError("resend: HTTP 503")
        with self.assertRaises(user_sinks.AlertsUnavailable):
            user_sinks.start_email_verification(self.api, user_id=uid, email="a@example.com", now=self.now,
                                                pepper=PEPPER, provider=mail)
        with self.assertRaises(ValidationFailed):
            user_sinks.start_email_verification(self.api, user_id=uid, email="not-an-email", now=self.now,
                                                pepper=PEPPER, provider=FakeEmail())

    # ------------------------------------------------------------------------------------------ gate
    def test_mandatory_contacts_gate(self) -> None:
        uid = self.user()
        svc = SimpleNamespace(store=self.store, now=lambda: self.now)
        with self.assertRaises(user_sinks.ContactsRequired) as cm:
            user_sinks.require_alert_contacts(self.api, svc, uid)
        self.assertEqual(cm.exception.code, "contacts_required")
        self.assertEqual(cm.exception.http_status, 409)
        self.assertEqual(sorted(cm.exception.details["missing"]), ["email", "telegram"])
        self.link(uid, self.chat())
        with self.assertRaises(user_sinks.ContactsRequired) as cm:
            user_sinks.require_alert_contacts(self.api, svc, uid)
        self.assertEqual(cm.exception.details["missing"], ["email"])
        self.verify_email(uid, f"g{self.tag}@example.com")
        user_sinks.require_alert_contacts(self.api, svc, uid)          # passes
        user_sinks.mark_telegram_blocked(self.exe, user_id=uid, now=self.now)
        with self.assertRaises(user_sinks.ContactsRequired):
            user_sinks.require_alert_contacts(self.api, svc, uid)      # blocked bot → no new subscriptions
        # fakes hook (API test world) takes precedence
        fake = SimpleNamespace(store=SimpleNamespace(alert_contacts_ready=lambda c, u: True), now=lambda: self.now)
        user_sinks.require_alert_contacts(None, fake, uid)

    # ------------------------------------------------------------------------------------------ prefs
    def test_prefs(self) -> None:
        uid = self.user()
        prefs.set_mutes(self.api, uid, {"trade_opened": True, "daily_pnl_summary": True}, self.now)
        prefs.set_mutes(self.api, uid, {"daily_pnl_summary": False}, self.now)
        self.assertEqual(prefs.muted_kinds(self.api, [uid]), {uid: {"trade_opened"}})
        with self.assertRaises(ValidationFailed):
            prefs.set_mutes(self.api, uid, {"agent_expired": True}, self.now)
        with self.assertRaises(DbError) as cm:   # the DB refuses too (CHECK)
            self.api.fetchall("""INSERT INTO alert_prefs (user_id, kind, muted) VALUES (CAST(:u AS uuid), 'balance_low', true)
                                 RETURNING kind""", {"u": uid})
        self.assertEqual(cm.exception.sqlstate, "23514")
        with self.assertRaises(DbError) as cm:   # nobody may DELETE
            self.api.fetchall("DELETE FROM alert_prefs WHERE user_id = CAST(:u AS uuid) RETURNING kind", {"u": uid})
        self.assertEqual(cm.exception.sqlstate, "42501")

    # ------------------------------------------------------------------------------------------ delivery
    def test_delivery_routing_mutes_idempotency(self) -> None:
        uid, chat, addr = self.ready_user()
        prefs.set_mutes(self.api, uid, {"trade_closed": True}, self.now)
        wallet = "0x" + "ab" * 20
        a_open = self.alert(uid, "trade_opened", payload={"coin": "xyz:SILVER", "side": "buy", "size": "2",
                                                          "avg_px": "30.1", "fees_micro": 60_000, "strategy": "SILVER"})
        a_closed = self.alert(uid, "trade_closed", payload={"coin": "xyz:SILVER", "side": "sell", "size": "2",
                                                            "avg_px": "31", "fees_micro": 60_000})
        a_bal = self.alert(uid, "balance_low", "warn", {"balance_micro": 4_000_000, "threshold_bps": 2000,
                                                        "need_micro": 20_000_000})
        a_agent = self.alert(uid, "agent_expired", "critical", {"master": wallet, "secret": "a" * 64})
        tg, mail = FakeTelegram(), FakeEmail()
        rep = self.run_worker(tg, mail)
        self.assertGreaterEqual(rep["sent_telegram"], 3)
        msgs = tg.to(chat)
        self.assertEqual(len(msgs), 3)                                     # opened, balance_low, agent_expired
        self.assertTrue(any("Trade opened: xyz:SILVER" in m for m in msgs))
        self.assertFalse(any("Trade closed" in m for m in msgs))           # muted
        self.assertTrue(any(m.startswith("URGENT: Agent approval expired") for m in msgs))
        mails = mail.to(addr)
        self.assertEqual(sorted(s for s, _ in mails),                      # email only for mandatory kinds
                         ["[aijalon] Agent approval expired — trading stopped", "[aijalon] Fee balance running low"])
        for text in msgs + [t for _, t in mails]:
            self.assertIsNone(ADDR_RE.search(text))
            self.assertNotIn("a" * 64, text)
        self.assertEqual(self.deliveries(a_open)["telegram"]["status"], "sent")
        self.assertEqual((self.deliveries(a_open)["email"]["status"], self.deliveries(a_open)["email"]["last_error"]),
                         ("skipped", "policy"))
        self.assertEqual(self.deliveries(a_closed)["telegram"]["last_error"], "muted")
        self.assertEqual({c: d["status"] for c, d in self.deliveries(a_bal).items()}, {"telegram": "sent", "email": "sent"})
        # idempotent: nothing is sent twice
        self.run_worker(tg, mail)
        self.assertEqual(len(tg.to(chat)), 3)
        self.assertEqual(len(mail.to(addr)), 2)
        # a terminal delivery row is final (DB guard)
        with self.assertRaises(DbError) as cm:
            self.exe.fetchall("""UPDATE alert_deliveries SET status = 'retry', next_attempt_at = now()
                                 WHERE alert_id = CAST(:a AS uuid) AND channel = 'telegram' RETURNING id""", {"a": a_bal})
        self.assertEqual(cm.exception.sqlstate, "AJ403")

    def test_delivery_retry_backoff_and_give_up(self) -> None:
        uid, chat, addr = self.ready_user()
        a = self.alert(uid, "withdrawal_sent", "info", {"amount_micro": 25_000_000})
        tg, mail = FakeTelegram(), FakeEmail()
        tg.fail[chat] = TransientSinkError("telegram: HTTP 502")
        self.run_worker(tg, mail)
        d = self.deliveries(a)
        self.assertEqual((d["telegram"]["status"], d["telegram"]["attempts"]), ("retry", 1))
        self.assertEqual(d["email"]["status"], "sent")
        # not due yet → no attempt
        self.run_worker(tg, mail, now=self.now + timedelta(seconds=30))
        self.assertEqual(self.deliveries(a)["telegram"]["attempts"], 1)
        # due → retried and sent
        del tg.fail[chat]
        self.run_worker(tg, mail, now=self.now + timedelta(minutes=2))
        self.assertEqual(self.deliveries(a)["telegram"]["status"], "sent")
        self.assertEqual(len(tg.to(chat)), 1)
        # permanent give-up after MAX_ATTEMPTS transient failures
        b = self.alert(uid, "withdrawal_sent", "info", {"amount_micro": 1})
        tg.fail[chat] = TransientSinkError("telegram: HTTP 500")
        t = self.now
        for _ in range(delivery.MAX_ATTEMPTS):
            self.run_worker(tg, mail, now=t)
            t += timedelta(hours=2)
        d = self.deliveries(b)["telegram"]
        self.assertEqual((d["status"], d["attempts"]), ("failed", delivery.MAX_ATTEMPTS))
        self.assertIn("gave up", d["last_error"])

    def test_bot_blocked_on_send(self) -> None:
        uid, chat, addr = self.ready_user()
        a = self.alert(uid, "trade_opened", payload={"coin": "BTC", "side": "buy", "size": "0.1", "avg_px": "60000"})
        b = self.alert(uid, "profit_share_charged", payload={"amount_micro": 1_000_000})
        tg, mail = FakeTelegram(), FakeEmail()
        tg.fail[chat] = TelegramBlocked("telegram: HTTP 403", "blocked")
        rep = self.run_worker(tg, mail)
        self.assertEqual(rep["blocked_users"], 1)
        got = sorted((self.deliveries(x)["telegram"]["status"], self.deliveries(x)["telegram"]["last_error"])
                     for x in (a, b))
        # the first attempt hits the 403; the other alert is then skipped (no working link) instead of retried
        self.assertEqual(got, [("failed", "blocked: blocked"), ("skipped", "no_telegram")])
        st = user_sinks.contact_status(self.api, uid, self.now)
        self.assertEqual(st.telegram, "blocked")
        self.assertTrue(st.entries_allowed)
        self.assertFalse(user_sinks.entries_allowed(self.exe, uid, self.now + timedelta(hours=24, seconds=1)))
        # the user is told by email (mandatory telegram_unreachable, email only)
        self.run_worker(tg, mail)
        subjects = [s for s, _ in mail.to(addr)]
        self.assertIn("[aijalon] Telegram alerts stopped", subjects)
        body = [t for s, t in mail.to(addr) if s == "[aijalon] Telegram alerts stopped"][0]
        self.assertIn("/#/alerts", body)
        self.assertEqual(tg.to(chat), [])

    def test_outbox_materialisation_and_ops(self) -> None:
        uid, chat, addr = self.ready_user()
        st_id, _, sub_id = self.strategy_sub(uid, ["BTC"])
        ins = """INSERT INTO events_outbox (user_id, kind, severity, payload, dedup_key)
                 VALUES (CAST(:u AS uuid), :k, CAST(:s AS alert_severity), CAST(:p AS jsonb), :d) RETURNING id"""
        e1 = self.api.fetchall(ins, {"u": uid, "k": "trade_closed", "s": "info", "d": f"t1:{self.tag}",
                                     "p": {"subscription_id": sub_id, "strategy_id": st_id, "coin": "BTC", "side": "sell",
                                           "size": "0.01", "avg_px": "61000", "fees_micro": 30_000,
                                           "builder_fee_micro": 6_000, "realized_pnl_micro": 1_030_000,
                                           "net_pnl_micro": 1_000_000, "position_after": "0"}})[0]["id"]
        e2 = self.api.fetchall(ins, {"u": uid, "k": "agent_expiring", "s": "info", "d": f"t2:{self.tag}",
                                     "p": {"days_left": 7, "valid_until_ms": 1790000000000, "master": "0x1234…abcd",
                                           "reapprove_path": "#/agents"}})[0]["id"]
        e3 = self.api.fetchall(ins, {"u": None, "k": "fill_after_settlement", "s": "critical", "d": f"t3:{self.tag}",
                                     "p": {"subscription_id": sub_id, "tid": 1}})[0]["id"]
        tg, mail = FakeTelegram(), FakeEmail()
        rep = self.run_worker(tg, mail)
        self.assertGreaterEqual(rep["materialized"], 3)
        rows = {r["dedup_key"]: r for r in self.su.fetchall(
            "SELECT id, dedup_key, kind, severity::text AS severity, payload, user_id FROM alerts WHERE dedup_key = ANY(:k)",
            {"k": [f"outbox:{e1}", f"outbox:{e1}/pnl", f"outbox:{e2}", f"outbox:{e3}"]})}
        self.assertEqual(rows[f"outbox:{e1}"]["payload"]["strategy"], f"Strat {self.tag}")   # enriched
        self.assertEqual(rows[f"outbox:{e1}/pnl"]["kind"], "trade_pnl")                        # derived
        self.assertEqual(rows[f"outbox:{e2}"]["severity"], "warn")                             # upgraded by policy
        self.assertIsNone(rows[f"outbox:{e3}"]["user_id"])
        delivered = self.su.fetchall("SELECT count(*) AS n FROM events_outbox WHERE id::text = ANY(:ids) AND delivered_at IS NOT NULL",
                                     {"ids": [str(e1), str(e2), str(e3)]})
        self.assertEqual(delivered[0]["n"], 3)
        msgs = tg.to(chat)
        self.assertTrue(any("Trade closed: BTC" in m and "Realized PnL: +$1.00" in m for m in msgs))
        self.assertTrue(any("Realized profit: +$1.00 on BTC" in m for m in msgs))
        self.assertTrue(any("Agent approval expires in 7 days" in m and "https://aijalon.trade/#/dashboard" in m
                            for m in msgs))
        self.assertEqual([s for s, _ in mail.to(addr)], ["[aijalon] Agent approval expires in 7 days"])
        # ops event (critical) → ops Telegram chat + ops email
        self.assertTrue(any("[ops]" in t for t in tg.to(-1001)))
        self.assertTrue(mail.to("ops@aijalon.test"))
        # re-run: nothing new
        n_tg, n_mail = len(tg.sent), len(mail.sent)
        self.run_worker(tg, mail)
        self.assertEqual((len(tg.sent), len(mail.sent)), (n_tg, n_mail))

    def test_fanout_kill_switch_to_affected_users(self) -> None:
        uid, chat, addr = self.ready_user()
        other, other_chat, _ = self.ready_user()
        coin = f"xyz:T{self.tag[:6].upper()}"
        self.strategy_sub(uid, [coin, "BTC"])
        self.strategy_sub(other, ["SOL"])
        self.store.insert_alert(self.api, user_id=None, severity="critical", kind="kill_switch_engaged",
                                payload={"key": f"kill_switch_market:{coin}", "by": "x", "reason": "internal note"})
        tg, mail = FakeTelegram(), FakeEmail()
        self.run_worker(tg, mail)
        mine = self.su.fetchall("SELECT kind, payload FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind = 'market_paused'",
                                {"u": uid})
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["payload"]["scope"], coin)
        self.assertEqual(self.su.fetchall("SELECT count(*) AS n FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind = 'market_paused'",
                                          {"u": other})[0]["n"], 0)
        self.assertTrue(any(f"Trading halted on {coin}" in m for m in tg.to(chat)))
        self.assertTrue(any("halted" in s for s, _ in mail.to(addr)))                 # mandatory → email too
        self.assertFalse(any("internal note" in m for m in tg.to(chat)))              # admin reason not leaked
        self.run_worker(tg, mail)
        self.assertEqual(len(self.su.fetchall("SELECT 1 AS x FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind = 'market_paused'",
                                              {"u": uid})), 1)

    def test_fanout_signals_stale_from_outbox(self) -> None:
        uid, chat, addr = self.ready_user()
        other, other_chat, _ = self.ready_user()
        coin = f"xyz:S{self.tag[:6].upper()}"
        self.strategy_sub(uid, [coin])
        self.strategy_sub(other, ["SOL"])
        # exactly what app/jobs_data/signals._emit_rejection writes (ops event, critical, coin + strategy_key)
        self.api.fetchall("""INSERT INTO events_outbox (user_id, kind, severity, payload, dedup_key)
                             VALUES (NULL, 'signals_stale', 'critical', CAST(:p AS jsonb), :d) RETURNING id""",
                          {"p": {"reason": "stale", "message": "feed too old", "strategy_key": "NOPE" + self.tag,
                                 "coin": coin}, "d": f"signals_stale:{coin}:{self.tag}"})
        tg, mail = FakeTelegram(), FakeEmail()
        self.run_worker(tg, mail)
        rows = self.su.fetchall("SELECT severity::text AS severity, payload FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind = 'signal_stale'",
                                {"u": uid})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["coin"], coin)
        self.assertEqual(self.su.fetchall("SELECT count(*) AS n FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind = 'signal_stale'",
                                          {"u": other})[0]["n"], 0)
        self.assertTrue(any(f"Strategy signal stale ({coin})" in m for m in tg.to(chat)))
        self.assertEqual(mail.to(addr), [])                                   # signal_stale: Telegram only
        self.assertTrue(any("[ops]" in t and "Signals stale" in t for t in tg.to(-1001)))   # ops paged

    def test_inapp_sink_rows_render_as_is(self) -> None:
        uid, chat, _ = self.ready_user()
        # a row written by notifier.InAppSink (executor / settlement): rendered text only
        self.alert(uid, "subscription_past_due", "warn", {"title": "Subscription past due",
                                                          "body": "Subscription x on SILVER could not be renewed.",
                                                          "coin": None, "key": "k"})
        tg = FakeTelegram()
        self.run_worker(tg, FakeEmail())
        self.assertTrue(any("Subscription x on SILVER could not be renewed." in m for m in tg.to(chat)))

    def test_send_test_alert(self) -> None:
        uid, chat, addr = self.ready_user()
        tg, mail = FakeTelegram(), FakeEmail()
        out = delivery.send_test_alert(self.api, user_id=uid, now=self.now, settings=_settings(), telegram=tg, email=mail)
        self.assertEqual(out, {"telegram": "sent", "email": "sent"})
        self.assertIn("Test alert", tg.to(chat)[0])
        tg.fail[chat] = TelegramBlocked("403", "blocked")
        out = delivery.send_test_alert(self.api, user_id=uid, now=self.now, settings=_settings(), telegram=tg, email=mail)
        self.assertEqual(out["telegram"], "blocked")
        self.assertEqual(user_sinks.contact_status(self.api, uid, self.now).telegram, "blocked")
        # the worker never re-sends test alerts
        n = len(tg.sent) + len(mail.sent)
        self.run_worker(FakeTelegram(), mail)
        self.assertEqual(len(mail.to(addr)), 2 + 1)          # 2 test mails + the telegram_unreachable notice
        self.assertEqual(len(tg.sent) + len(mail.sent), n + 1)
        out = delivery.send_test_alert(self.api, user_id=self.user(), now=self.now, settings=_settings(),
                                       telegram=FakeTelegram(), email=FakeEmail())
        self.assertEqual(out, {"telegram": "not_linked", "email": "not_verified"})

    # ------------------------------------------------------------------------------------------ low balance
    def test_low_balance_via_ledger_ops_and_sql_hook(self) -> None:
        from app.api import ledger_ops
        from app.domain import billing
        from app.domain.fees import plan_price, subscription_split
        from app.ledger import service

        uid = self.user()
        self.strategy_sub(uid, ["BTC"], price=20_000_000)       # need = $20 (free plan)
        store = self.store

        class Ledger:
            def ensure_account(self, conn, code):
                kind, nn, owner = ledger_ops.account_spec(code)
                service.ensure_account(conn, code, kind, owner, non_negative=nn)

            def post(self, conn, *, idempotency_key, kind, memo, entries, created_by):
                return service.post_transaction(conn, idempotency_key, kind, memo, entries, created_by).id

            def balance(self, conn, code):
                return service.get_balance(conn, code)

        class Notifier:
            def notify(self, conn, *, user_id, severity, kind, payload):
                store.insert_alert(conn, user_id=user_id, severity=severity, kind=kind, payload=payload)

        svc = SimpleNamespace(store=store, ledger=Ledger(), notifier=Notifier(), domain=SimpleNamespace(
            estimate_monthly_need=billing.estimate_monthly_need, plan_price=plan_price,
            subscription_split=subscription_split))
        fb = ledger_ops.fee_balance(uid)
        ledger_ops.post(self.api, svc, key=f"t:{self.tag}:top", kind="deposit", memo="t", created_by="test",
                        entries=[("stripe:clearing", 30_000_000), (fb, -30_000_000)])
        ledger_ops.post(self.api, svc, key=f"t:{self.tag}:d1", kind="plan_purchase", memo="t", created_by="test",
                        entries=[(fb, 12_000_000), ("platform:revenue:plans", -12_000_000)])      # 18 > 10: none
        kinds = lambda: sorted(r["kind"] + ":" + str(r["payload"]["threshold_bps"]) for r in self.su.fetchall(  # noqa: E731
            "SELECT kind, payload FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind LIKE 'balance_%'", {"u": uid}))
        self.assertEqual(kinds(), [])
        ledger_ops.post(self.api, svc, key=f"t:{self.tag}:d2", kind="plan_purchase", memo="t", created_by="test",
                        entries=[(fb, 10_000_000), ("platform:revenue:plans", -10_000_000)])      # 8 ≤ 10: 50 %
        self.assertEqual(kinds(), ["balance_low:5000"])
        # idempotent replay of the same ledger key: balance unchanged → no alert
        ledger_ops.post(self.api, svc, key=f"t:{self.tag}:d2", kind="plan_purchase", memo="t", created_by="test",
                        entries=[(fb, 10_000_000), ("platform:revenue:plans", -10_000_000)])
        self.assertEqual(kinds(), ["balance_low:5000"])
        ledger_ops.post(self.api, svc, key=f"t:{self.tag}:d3", kind="plan_purchase", memo="t", created_by="test",
                        entries=[(fb, 8_000_000), ("platform:revenue:plans", -8_000_000)])        # 0: 20 % + 0 %
        self.assertEqual(kinds(), ["balance_empty:0", "balance_low:2000", "balance_low:5000"])
        # SQL path (settlement / executor role): dedup on (user, threshold, prev, new)
        uid2 = self.user()
        self.strategy_sub(uid2, ["BTC"], price=20_000_000)
        out = delivery.on_balance_changed(self.exe, uid2, 15_000_000, 3_000_000, now=self.now)
        self.assertEqual(out, ["balance_low", "balance_low"])
        delivery.on_balance_changed(self.exe, uid2, 15_000_000, 3_000_000, now=self.now)
        self.assertEqual(self.su.fetchall("SELECT count(*) AS n FROM alerts WHERE user_id = CAST(:u AS uuid)",
                                          {"u": uid2})[0]["n"], 2)
        self.assertEqual(delivery.on_balance_changed(self.exe, uid2, 3_000_000, 50_000_000, now=self.now), [])

    # ------------------------------------------------------------------------------------------ daily PnL
    def test_daily_pnl_summary(self) -> None:
        uid = self.user()
        _, _, sub = self.strategy_sub(uid, ["BTC"])
        day = (self.now - timedelta(days=1)).date()
        t = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
        addr = "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
        for tid, cpm, fee in ((1, 2_000_000, 100_000), (2, 0, 50_000)):
            self.exe.fetchall("""INSERT INTO fills (subscription_id, trading_address, coin, tid, px, sz, side,
                                                    closed_pnl_micro, fee_micro, time)
                                 VALUES (CAST(:s AS uuid), :a, 'BTC', :tid, 60000, 0.01, 'sell', :c, :f,
                                         CAST(:t AS timestamptz)) RETURNING id""",
                              {"s": sub, "a": addr, "tid": tid, "c": cpm, "f": fee, "t": t})
        now = datetime(self.now.year, self.now.month, self.now.day, 0, 15, tzinfo=timezone.utc)
        out = delivery.emit_daily_pnl_summaries(RunnerDb(self.exe), now, day=day)
        self.assertGreaterEqual(out["created"], 1)
        rows = self.su.fetchall("""SELECT payload FROM alerts WHERE user_id = CAST(:u AS uuid)
                                   AND kind = 'daily_pnl_summary'""", {"u": uid})
        self.assertEqual(len(rows), 1)
        p = rows[0]["payload"]
        self.assertEqual((p["realized_pnl_micro"], p["fees_micro"], p["fills"], p["closed_trades"]),
                         (1_850_000, 150_000, 2, 1))
        self.assertEqual(p["date"], day.isoformat())
        delivery.emit_daily_pnl_summaries(RunnerDb(self.exe), now, day=day)
        self.assertEqual(len(self.su.fetchall("SELECT 1 AS x FROM alerts WHERE user_id = CAST(:u AS uuid) AND kind = 'daily_pnl_summary'",
                                              {"u": uid})), 1)


if __name__ == "__main__":
    unittest.main()
