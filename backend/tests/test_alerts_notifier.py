"""Notifier: severity routing, dedupe, rate limits, redaction, retries, auto-pause, HTTP request formation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.alerts.notifier import (  # noqa: E402
    Alert,
    EmailSink,
    InAppSink,
    InMemoryMetrics,
    Notifier,
    PermanentSinkError,
    ResendProvider,
    SendGridProvider,
    Severity,
    TelegramSink,
    TransientSinkError,
    mask_address,
    render,
    sanitize_text,
)

ADDR = "0x1234567890abcdef1234567890abcdef1234abcd"
PRIVKEY = "0x" + "9f" * 32


class Repo:
    def __init__(self):
        self.rows = []

    def insert_alert(self, user_id, severity, kind, payload):
        self.rows.append((user_id, severity, kind, payload))


class Flags:
    def __init__(self, fail=False):
        self.paused, self.fail = [], fail

    def set_market_paused(self, coin, reason):
        if self.fail:
            raise RuntimeError("db down")
        self.paused.append((coin, reason))


class Contacts:
    def email_for(self, user_id, alert):
        return f"{user_id}@example.com"


class FakeProvider:
    name = "fake"

    def __init__(self, fail_times=0, exc=TransientSinkError):
        self.sent, self.fail_times, self.exc, self.attempts = [], fail_times, exc, 0

    def send(self, to, subject, text):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.exc("boom")
        self.sent.append((to, subject, text))


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {"ok": True}, headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return self.responses.pop(0) if self.responses else FakeResp()


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(clock=None, provider=None, tg_session=None, flags=None, **kw):
    repo = Repo()
    provider = provider or FakeProvider()
    session = tg_session or FakeSession()
    sleeps = []
    n = Notifier(
        in_app=InAppSink(repo),
        email=EmailSink(provider),
        telegram=TelegramSink("123:TOKENabc", "-100999", session=session),
        contacts=Contacts(),
        ops_emails=["ops@aijalon.trade"],
        flags=flags if flags is not None else Flags(),
        metrics=InMemoryMetrics(),
        clock=clock or Clock(),
        sleep=sleeps.append,
        **kw,
    )
    return n, repo, provider, session, sleeps


class RoutingTests(unittest.TestCase):
    def test_info_in_app_only(self):
        n, repo, prov, tg, _ = make()
        r = n.notify(Alert("topup_credited", Severity.INFO, "u1", data={"amount_micro": 10_000_000, "method": "USDC"}))
        self.assertEqual(len(repo.rows), 1)
        self.assertEqual(prov.sent, [])
        self.assertEqual(tg.calls, [])
        self.assertEqual(r.delivered, {"in_app": True})

    def test_warn_in_app_and_user_email(self):
        n, repo, prov, tg, _ = make()
        r = n.notify(Alert("balance_low", Severity.WARN, "u1", data={"balance_micro": 3_000_000, "pct": 20}))
        self.assertEqual(len(repo.rows), 1)
        self.assertEqual([s[0] for s in prov.sent], ["u1@example.com"])
        self.assertEqual(tg.calls, [])
        self.assertTrue(r.delivered["email_user"])

    def test_warn_ops_alert_goes_to_ops_email(self):
        n, _, prov, tg, _ = make()
        n.notify(Alert("topup_held", Severity.WARN, None, data={"amount_micro": 5_000_000, "method": "USDC", "reason": "x"}))
        self.assertEqual([s[0] for s in prov.sent], ["ops@aijalon.trade"])
        self.assertEqual(tg.calls, [])

    def test_critical_everything_and_auto_pause(self):
        flags = Flags()
        n, repo, prov, tg, _ = make(flags=flags)
        r = n.notify(Alert("mark_oracle_divergence", Severity.CRITICAL, None, coin="xyz:SILVER", data={"deviation_bps": 350}))
        self.assertEqual(len(repo.rows), 1)
        self.assertEqual([s[0] for s in prov.sent], ["ops@aijalon.trade"])
        self.assertEqual(len(tg.calls), 1)
        self.assertEqual(flags.paused[0][0], "xyz:SILVER")
        self.assertTrue(r.paused)

    def test_critical_user_alert_emails_user_and_ops(self):
        n, _, prov, tg, _ = make()
        n.notify(Alert("agent_approval_changed", Severity.CRITICAL, "u7", data={"address": ADDR, "detail": "revoked"}))
        self.assertEqual(sorted(s[0] for s in prov.sent), ["ops@aijalon.trade", "u7@example.com"])
        self.assertEqual(len(tg.calls), 1)

    def test_critical_without_coin_does_not_pause(self):
        flags = Flags()
        n, *_ = make(flags=flags)
        n.notify(Alert("reconciliation_mismatch", Severity.CRITICAL, None, data={"scope": "builder"}))
        self.assertEqual(flags.paused, [])


class DedupeRateLimitTests(unittest.TestCase):
    def test_dedupe_window(self):
        clock = Clock()
        n, repo, *_ = make(clock=clock)
        a = Alert("balance_low", Severity.INFO, "u1", data={"balance_micro": 1})
        self.assertFalse(n.notify(a).deduped)
        clock.t += 29 * 60
        self.assertTrue(n.notify(a).deduped)
        clock.t += 2 * 60
        self.assertFalse(n.notify(a).deduped)
        self.assertEqual(len(repo.rows), 2)

    def test_distinct_keys_not_deduped(self):
        n, repo, *_ = make()
        n.notify(Alert("balance_low", Severity.INFO, "u1"))
        n.notify(Alert("balance_low", Severity.INFO, "u2"))
        n.notify(Alert("balance_low", Severity.INFO, "u1", key="custom"))
        self.assertEqual(len(repo.rows), 3)

    def test_auto_pause_runs_even_when_deduped(self):
        flags = Flags()
        n, *_ , tg, _ = make(flags=flags)
        a = Alert("mark_oracle_divergence", Severity.CRITICAL, None, coin="BTC")
        n.notify(a)
        r = n.notify(a)
        self.assertTrue(r.deduped)
        self.assertEqual(len(flags.paused), 2)
        self.assertEqual(len(tg.calls), 1)

    def test_user_email_rate_limit_but_critical_bypasses(self):
        n, _, prov, _, _ = make(email_per_user_per_hour=2)
        for i in range(4):
            n.notify(Alert("balance_low", Severity.WARN, "u1", key=f"k{i}"))
        self.assertEqual(len(prov.sent), 2)
        r = n.notify(Alert("mfa_reset", Severity.CRITICAL, "u1"))
        self.assertTrue(r.delivered.get("email_user"))

    def test_telegram_global_rate_limit(self):
        n, _, _, tg, _ = make(telegram_per_minute=1)
        n.notify(Alert("kill_switch", Severity.CRITICAL, None, key="a"))
        r = n.notify(Alert("kill_switch", Severity.CRITICAL, None, key="b"))
        self.assertEqual(len(tg.calls), 1)
        self.assertEqual(r.skipped["telegram"], "rate limited")


class RedactionTests(unittest.TestCase):
    def test_mask_address(self):
        self.assertEqual(mask_address(ADDR), "0x1234…abcd")

    def test_sanitize(self):
        s = sanitize_text(f"key {PRIVKEY} addr {ADDR} mail jane.doe@example.com sk_live_abcdefghijk\x00")
        self.assertNotIn("9f9f9f9f", s)
        self.assertNotIn(ADDR, s)
        self.assertIn("0x1234…abcd", s)
        self.assertIn("j***@example.com", s)
        self.assertNotIn("jane.doe", s)
        self.assertNotIn("sk_live_abcdefghijk", s)
        self.assertNotIn("\x00", s)

    def test_rendered_and_delivered_text_is_redacted(self):
        n, repo, prov, tg, _ = make()
        n.notify(Alert("withdrawal_requested", Severity.CRITICAL, "u1",
                       data={"amount_micro": 1_234_560_000, "address": ADDR, "note": PRIVKEY}))
        n.notify(Alert("unknown_kind", Severity.CRITICAL, None, data={"detail": f"leak {PRIVKEY} {ADDR}"}))
        blobs = [str(r) for r in repo.rows] + [str(s) for s in prov.sent] + [str(c["json"]) for c in tg.calls]
        for b in blobs:
            self.assertNotIn(ADDR, b)
            self.assertNotIn("9f" * 32, b)
        self.assertIn("$1,234.56", prov.sent[0][2])
        self.assertIn("0x1234…abcd", prov.sent[0][2])

    def test_missing_template_param(self):
        r = render(Alert("balance_low", Severity.WARN, "u1"))
        self.assertIn("?", r.body)

    def test_format_injection_not_evaluated(self):
        r = render(Alert("kill_switch", Severity.WARN, None, data={"scope": "{reason.__class__}", "reason": "x"}))
        self.assertIn("{reason.__class__}", r.body)


class ResilienceTests(unittest.TestCase):
    def test_transient_retry_with_backoff(self):
        prov = FakeProvider(fail_times=2)
        n, _, _, _, sleeps = make(provider=prov)
        r = n.notify(Alert("balance_low", Severity.WARN, "u1"))
        self.assertTrue(r.delivered["email_user"])
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_transient_exhausted_no_raise(self):
        prov = FakeProvider(fail_times=10)
        n, _, _, _, sleeps = make(provider=prov)
        r = n.notify(Alert("balance_low", Severity.WARN, "u1"))
        self.assertFalse(r.delivered["email_user"])
        self.assertEqual(prov.attempts, 3)
        self.assertTrue(r.errors)
        self.assertTrue(r.delivered["in_app"])

    def test_permanent_no_retry(self):
        prov = FakeProvider(fail_times=10, exc=PermanentSinkError)
        n, _, _, _, sleeps = make(provider=prov)
        r = n.notify(Alert("balance_low", Severity.WARN, "u1"))
        self.assertEqual(prov.attempts, 1)
        self.assertEqual(sleeps, [])
        self.assertFalse(r.delivered["email_user"])

    def test_unexpected_exception_in_sink_does_not_raise(self):
        class Broken:
            def insert_alert(self, *a):
                raise ZeroDivisionError

        n = Notifier(in_app=InAppSink(Broken()), sleep=lambda s: None)
        r = n.notify(Alert("balance_low", Severity.INFO, "u1"))
        self.assertFalse(r.delivered["in_app"])

    def test_auto_pause_failure_does_not_raise(self):
        n, _, _, tg, _ = make(flags=Flags(fail=True))
        r = n.notify(Alert("oi_spike", Severity.CRITICAL, None, coin="HYPE"))
        self.assertFalse(r.paused)
        self.assertEqual(len(tg.calls), 1)
        self.assertTrue(any("auto_pause" in e for e in r.errors))

    def test_garbage_alert_does_not_raise(self):
        n, *_ = make()
        r = n.notify("not an alert")  # type: ignore[arg-type]
        self.assertTrue(r.errors)

    def test_telegram_429_retry_after(self):
        session = FakeSession([FakeResp(429, {"ok": False, "parameters": {"retry_after": 3}}), FakeResp(200, {"ok": True})])
        n, _, _, _, sleeps = make(tg_session=session)
        r = n.notify(Alert("kill_switch", Severity.CRITICAL, None))
        self.assertTrue(r.delivered["telegram"])
        self.assertEqual(sleeps, [3.0])


class HttpFormationTests(unittest.TestCase):
    def test_telegram_request(self):
        s = FakeSession()
        TelegramSink("123:SECRET", "-100777", session=s).send(render(Alert("kill_switch", Severity.CRITICAL, None,
                                                                             data={"scope": "BTC", "reason": "oracle"})), "k1")
        c = s.calls[0]
        self.assertEqual(c["url"], "https://api.telegram.org/bot123:SECRET/sendMessage")
        self.assertEqual(c["json"]["chat_id"], "-100777")
        self.assertTrue(c["json"]["disable_web_page_preview"])
        self.assertNotIn("parse_mode", c["json"])
        self.assertIn("[CRITICAL] Kill switch engaged", c["json"]["text"])
        self.assertTrue(c["timeout"])

    def test_telegram_truncates_and_checks_ok(self):
        s = FakeSession([FakeResp(200, {"ok": False})])
        sink = TelegramSink("t", "c", session=s)
        with self.assertRaises(PermanentSinkError):
            sink.send_text("x" * 5000)
        self.assertEqual(len(s.calls[0]["json"]["text"]), 4096)

    def test_telegram_errors(self):
        with self.assertRaises(TransientSinkError):
            TelegramSink("t", "c", session=FakeSession([FakeResp(502, {})])).send_text("x")
        with self.assertRaises(PermanentSinkError):
            TelegramSink("t", "c", session=FakeSession([FakeResp(403, {})])).send_text("x")

        class Boom:
            def post(self, *a, **k):
                raise ConnectionError("down")

        with self.assertRaises(TransientSinkError):
            TelegramSink("t", "c", session=Boom()).send_text("x")

    def test_resend_request(self):
        s = FakeSession()
        ResendProvider("re_key123", "alerts@aijalon.trade", session=s).send("a@b.co", "Subj", "Body")
        c = s.calls[0]
        self.assertEqual(c["url"], "https://api.resend.com/emails")
        self.assertEqual(c["headers"]["Authorization"], "Bearer re_key123")
        self.assertEqual(c["json"], {"from": "alerts@aijalon.trade", "to": ["a@b.co"], "subject": "Subj", "text": "Body"})

    def test_sendgrid_request(self):
        s = FakeSession()
        SendGridProvider("SG.key", "alerts@aijalon.trade", session=s).send("a@b.co", "Subj", "Body")
        c = s.calls[0]
        self.assertEqual(c["url"], "https://api.sendgrid.com/v3/mail/send")
        self.assertEqual(c["headers"]["Authorization"], "Bearer SG.key")
        self.assertEqual(c["json"]["personalizations"], [{"to": [{"email": "a@b.co"}]}])
        self.assertEqual(c["json"]["content"], [{"type": "text/plain", "value": "Body"}])

    def test_email_provider_missing_key(self):
        with self.assertRaises(PermanentSinkError):
            ResendProvider("", "x@y.z", session=FakeSession()).send("a@b.co", "s", "t")

    def test_email_sink_subject_and_footer(self):
        prov = FakeProvider()
        EmailSink(prov).send_to("a@b.co", render(Alert("mfa_reset", Severity.CRITICAL, "u1")))
        to, subj, text = prov.sent[0]
        self.assertEqual(subj, "[aijalon] Two-factor authentication reset")
        self.assertIn("never ask for your seed phrase", text)


if __name__ == "__main__":
    unittest.main()
