"""Unit tests (no DB) for the user-alert modules: routing policy, mandatory kinds ↔ migration CHECK, templates
(no secrets / full addresses), Telegram Bot API error classification, webhook secret check, low-balance math."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.alerts import prefs  # noqa: E402
from app.alerts.delivery import low_balance_alerts  # noqa: E402
from app.alerts.notifier import PermanentSinkError, TransientSinkError  # noqa: E402
from app.alerts.telegram_bot import handle_update, secret_ok, token_hash  # noqa: E402
from app.alerts.user_sinks import TelegramBlocked, TelegramBotApi, normalize_email  # noqa: E402
from app.alerts.user_templates import USER_TEMPLATES, render_user_alert, telegram_text  # noqa: E402
from app.errors import ValidationFailed  # noqa: E402

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0007_alerts.sql"
ADDR = "0x" + "ab12" * 10
KEY = "f" * 64
ORIGIN = "https://aijalon.trade"


class FakeResp:
    def __init__(self, status: int, body: object = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class RoutingTest(unittest.TestCase):
    def test_email_policy(self) -> None:
        # trades + daily PnL: Telegram only (SPEC §12 email volume policy)
        for k in ("trade_opened", "trade_closed", "trade_resized", "trade_pnl", "daily_pnl_summary"):
            self.assertEqual(prefs.route(k, "info"), (True, False, False), k)
        # mandatory → both, locked
        for k in ("agent_expiring", "agent_expired", "builder_approval_missing", "balance_low", "balance_empty",
                  "subscription_past_due", "subscription_reduce_only", "stripe_refund", "stripe_dispute",
                  "withdrawal_requested", "withdrawal_sent", "new_device_login", "mfa_changed", "market_paused"):
            self.assertEqual(prefs.route(k, "info"), (True, True, True), k)
        # money but mutable
        self.assertEqual(prefs.route("profit_share_charged", "info"), (True, True, False))
        self.assertEqual(prefs.route("topup_credited", "info"), (True, True, False))
        # unknown kinds: Telegram always, email only when critical
        self.assertEqual(prefs.route("something_new", "warn"), (True, False, False))
        self.assertEqual(prefs.route("something_new", "critical"), (True, True, False))
        # telegram_unreachable goes by email only; test alerts are sent inline by the API
        self.assertEqual(prefs.route("telegram_unreachable", "warn"), (False, True, True))
        self.assertEqual(prefs.route("test_alert", "info")[:2], (False, False))

    def test_mandatory_kinds_match_migration_check(self) -> None:
        sql = MIGRATION.read_text()
        block = sql[sql.index("alert_prefs_mandatory_unmutable"):]
        block = block[: block.index("))")]
        in_sql = set(re.findall(r"'([a-z_]+)'", block))
        self.assertEqual(in_sql, set(prefs.MANDATORY_KINDS))

    def test_prefs_view_locks_mandatory(self) -> None:
        view = prefs.prefs_view({"trade_opened", "balance_low"})
        by = {r["kind"]: r for r in view}
        self.assertTrue(by["trade_opened"]["muted"])
        self.assertFalse(by["balance_low"]["muted"])          # mandatory can never show as muted
        self.assertTrue(by["balance_low"]["mandatory"])
        self.assertEqual(by["trade_opened"]["channels"], ["in_app", "telegram"])
        self.assertEqual(by["balance_low"]["channels"], ["in_app", "telegram", "email"])
        self.assertNotIn("test_alert", by)
        groups = [r["group"] for r in view]
        self.assertEqual(groups, sorted(groups, key=[g for g, _ in prefs.GROUPS].index))

    def test_set_mutes_validation(self) -> None:
        class NoDb:
            def fetchall(self, sql, params=None):
                raise AssertionError("must validate before touching the DB")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValidationFailed):
            prefs.set_mutes(NoDb(), "u", {"balance_low": True}, now)
        with self.assertRaises(ValidationFailed):
            prefs.set_mutes(NoDb(), "u", {"no_such_kind": True}, now)
        with self.assertRaises(ValidationFailed):
            prefs.set_mutes(NoDb(), "u", {"test_alert": True}, now)
        with self.assertRaises(ValidationFailed):
            prefs.set_mutes(NoDb(), "u", {}, now)


class TemplateTest(unittest.TestCase):
    def test_every_listed_kind_has_a_template(self) -> None:
        for kind, spec in prefs.CATALOG.items():
            self.assertIn(kind, USER_TEMPLATES, kind)

    def test_templates_render_with_empty_and_hostile_payloads(self) -> None:
        hostile = {"strategy": f"S {ADDR}", "coin": "BTC", "side": "buy", "size": "1", "avg_px": "2",
                   "fee_micro": 1, "wallet": ADDR, "email": "someone@example.com", "scope": f"x {KEY}",
                   "country": "MY", "amount_micro": 5_000_000, "request_id": KEY, "reason": "stopped",
                   "lines": [f"line {ADDR}"], "date": "2026-09-29", "realized_pnl_micro": -1_230_000,
                   "_source": "outbox", "token": "abcdefghijklmnopqrstuvwxyz012345"}
        for kind in USER_TEMPLATES:
            for payload in ({}, hostile):
                title, body = render_user_alert(kind, "warn", payload, web_origin=ORIGIN)
                text = telegram_text(title, body, "warn")
                self.assertTrue(title and body, kind)
                self.assertNotIn(ADDR, text, kind)
                self.assertNotIn(KEY, text, kind)
                self.assertNotIn("someone@example.com", text, kind)
                self.assertNotIn("outbox", text, kind)

    def test_specific_texts(self) -> None:
        t, b = render_user_alert("trade_opened", "info", {"strategy": "SILVER", "coin": "xyz:SILVER", "side": "buy",
                                                          "size": "3.2", "avg_px": "31.05", "fee_micro": 45_000},
                                 web_origin=ORIGIN)
        self.assertEqual(t, "Trade opened: xyz:SILVER")
        self.assertIn("Buy 3.2 xyz:SILVER @ avg 31.05", b)
        self.assertIn("Fees: $0.04", b)
        t, b = render_user_alert("trade_pnl", "info", {"strategy": "SILVER", "coin": "BTC",
                                                       "realized_pnl_micro": -2_500_000}, web_origin=ORIGIN)
        self.assertEqual(t, "Realized loss: -$2.50 on BTC")
        t, b = render_user_alert("agent_expiring", "warn", {"days_left": 1, "expires_at": "2026-10-01",
                                                            "wallet": ADDR}, web_origin=ORIGIN)
        self.assertEqual(t, "Agent approval expires in 1 day")
        self.assertIn("https://aijalon.trade/#/dashboard", b)
        self.assertIn("0xab12…ab12", b)
        t, b = render_user_alert("agent_expired", "critical", {}, web_origin=ORIGIN)
        self.assertIn("https://aijalon.trade/#/dashboard", b)
        t, b = render_user_alert("balance_low", "warn", {"balance_micro": 4_000_000, "threshold_bps": 2000,
                                                         "need_micro": 20_000_000}, web_origin=ORIGIN)
        self.assertIn("$4.00", b)
        self.assertIn("20%", b)
        t, b = render_user_alert("daily_pnl_summary", "info", {"date": "2026-09-29", "realized_pnl_micro": 1_500_000,
                                                               "fees_micro": 20_000, "fills": 3, "closed_trades": 1,
                                                               "lines": ["SILVER: +$1.50"]}, web_origin=ORIGIN)
        self.assertEqual(t, "Daily PnL 2026-09-29: +$1.50")
        self.assertIn("SILVER: +$1.50", b)
        t, b = render_user_alert("market_paused", "critical", {"scope": "xyz:SILVER", "cause": "kill switch"},
                                 web_origin=ORIGIN)
        self.assertIn("halted", t)
        # unknown kind → notifier generic rendering, sanitised
        t, b = render_user_alert("brand_new_kind", "warn", {"x": ADDR}, web_origin=ORIGIN)
        self.assertEqual(t, "Brand new kind")
        self.assertNotIn(ADDR, b)
        self.assertTrue(telegram_text("T", "B", "critical").startswith("URGENT: "))


class TelegramApiTest(unittest.TestCase):
    def _api(self, *responses: object) -> tuple[TelegramBotApi, FakeSession]:
        s = FakeSession(*responses)
        return TelegramBotApi("123:SECRET", session=s), s

    def test_ok_and_payload(self) -> None:
        api, s = self._api(FakeResp(200, {"ok": True, "result": {}}))
        api.send_message(42, "hello")
        url, body = s.calls[0]
        self.assertTrue(url.endswith("/bot123:SECRET/sendMessage"))
        self.assertEqual(body, {"chat_id": 42, "text": "hello", "disable_web_page_preview": True})
        self.assertNotIn("parse_mode", body)

    def test_classification(self) -> None:
        api, _ = self._api(FakeResp(403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}))
        with self.assertRaises(TelegramBlocked) as cm:
            api.send_message(1, "x")
        self.assertEqual(cm.exception.reason, "blocked")
        api, _ = self._api(FakeResp(400, {"ok": False, "description": "Bad Request: chat not found"}))
        with self.assertRaises(TelegramBlocked) as cm:
            api.send_message(1, "x")
        self.assertEqual(cm.exception.reason, "chat_not_found")
        api, _ = self._api(FakeResp(429, {"ok": False, "parameters": {"retry_after": 7}}))
        with self.assertRaises(TransientSinkError) as cm2:
            api.send_message(1, "x")
        self.assertEqual(cm2.exception.retry_after, 7.0)
        for resp in (FakeResp(502, ValueError("html")), ConnectionError("boom")):
            api, _ = self._api(resp)
            with self.assertRaises(TransientSinkError):
                api.send_message(1, "x")
        api, _ = self._api(FakeResp(401, {"ok": False}))
        with self.assertRaises(PermanentSinkError) as cm3:
            api.send_message(1, "x")
        self.assertNotIsInstance(cm3.exception, TelegramBlocked)
        self.assertNotIn("SECRET", str(cm3.exception))
        with self.assertRaises(PermanentSinkError):
            TelegramBotApi("").send_message(1, "x")

    def test_long_text_truncated(self) -> None:
        api, s = self._api(FakeResp(200, {"ok": True}))
        api.send_message(1, "x" * 5000)
        self.assertEqual(len(s.calls[0][1]["text"]), 4096)


class WebhookPureTest(unittest.TestCase):
    def test_secret_ok(self) -> None:
        self.assertTrue(secret_ok("s" * 64, "s" * 64))
        self.assertFalse(secret_ok("s" * 63, "s" * 64))
        self.assertFalse(secret_ok(None, "s" * 64))
        self.assertFalse(secret_ok("", ""))
        self.assertFalse(secret_ok("anything", ""))          # unconfigured → reject everything
        self.assertFalse(secret_ok("x" * 300, "x" * 300))

    def test_odd_updates_are_ignored_without_db(self) -> None:
        class NoDb:
            def fetchall(self, sql, params=None):
                raise AssertionError("no DB access expected")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for upd in (None, [], {}, {"message": "x"}, {"message": {"chat": {"id": 1, "type": "private"}}},
                    {"edited_message": {"text": "/stop"}}, {"message": {"chat": {"id": "abc"}, "text": "/stop"}},
                    {"my_chat_member": {"chat": {"id": 5, "type": "group"}, "new_chat_member": {"status": "kicked"}}}):
            self.assertIsNone(handle_update(NoDb(), upd, now))
        # a group chat gets a polite reply and no DB access
        r = handle_update(NoDb(), {"message": {"chat": {"id": -5, "type": "group"}, "text": "/start abc"}}, now)
        self.assertEqual(r["chat_id"], -5)
        # malformed token → bad-token reply, no DB access
        r = handle_update(NoDb(), {"message": {"chat": {"id": 7, "type": "private"}, "text": "/start bad token!"}}, now)
        self.assertIn("expired", r["text"])
        self.assertEqual(len(token_hash("x")), 64)


class MiscTest(unittest.TestCase):
    def test_normalize_email(self) -> None:
        self.assertEqual(normalize_email("  Foo.Bar@Example.COM "), "Foo.Bar@example.com")
        self.assertEqual(normalize_email("abc123@privaterelay.appleid.com"), "abc123@privaterelay.appleid.com")
        for bad in ("", "no-at", "a@b", "a b@c.com", "a@@b.com", "x" * 250 + "@a.com", None, 5, "a@b.c\nx@y.com"):
            with self.assertRaises(ValidationFailed, msg=repr(bad)):
                normalize_email(bad)

    def test_low_balance_alerts(self) -> None:
        need = 20_000_000
        self.assertEqual(low_balance_alerts("u", 30_000_000, 25_000_000, need), [])
        a = low_balance_alerts("u", 30_000_000, 3_000_000, need)
        self.assertEqual([x["kind"] for x in a], ["balance_low", "balance_low"])
        self.assertEqual([x["payload"]["threshold_bps"] for x in a], [5000, 2000])
        z = low_balance_alerts("u", 1_000_000, 0, need)
        self.assertEqual([(x["kind"], x["severity"]) for x in z], [("balance_empty", "critical")])
        self.assertEqual(low_balance_alerts("u", 30_000_000, 0, 0), [])     # no need → no alerts
        self.assertNotEqual(a[0]["dedup_key"], a[1]["dedup_key"])


if __name__ == "__main__":
    unittest.main()
