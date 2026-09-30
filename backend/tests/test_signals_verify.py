"""Signal feed verification (app.strategies.signals): signature, strict schema, registry, time, continuity, fetch."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone

try:  # the cryptography native backend is broken on some dev boxes (pyo3 panic); never skip in CI
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
except BaseException as e:  # noqa: BLE001 - pyo3 PanicException derives from BaseException
    if isinstance(e, (KeyboardInterrupt, SystemExit)) or os.environ.get("CI"):
        raise
    raise unittest.SkipTest(f"cryptography unavailable: {type(e).__name__}") from None

from app.strategies import signals as S

UTC = timezone.utc
NOW = datetime(2026, 9, 30, 0, 45, tzinfo=UTC)
SCRIPT = "e60119a7222c352085cf6753be7231e05a201ba2ecb495812d4671fc44a942ed"
GOLD_SCRIPT = "a" * 64


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def engine_of(hashes: dict) -> str:
    return hashlib.sha256(canon(hashes)).hexdigest()


def payload(*, weight=1, last_action="BUY", last_action_date="2026-07-01", market="xyz:SILVER", status="trades",
            as_of="2026-09-29", generated_at="2026-09-30T00:41:07Z", extra=None, engine=None, script=SCRIPT):
    strategies = {"silver": {"target_weight": weight, "last_action": last_action, "last_action_date": last_action_date,
                             "market": market, "status": status, "script_sha256": script}}
    strategies.update(extra or {})
    return {"as_of": as_of, "generated_at": generated_at,
            "engine_sha256": engine or engine_of({k: v["script_sha256"] for k, v in strategies.items()}),
            "strategies": strategies}


def pub_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


class Base(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.pub = pub_b64(self.key)

    def signed(self, obj=None, body: bytes | None = None, key=None):
        body = canon(obj if obj is not None else payload()) if body is None else body
        return body, base64.b64encode((key or self.key).sign(body)).decode()

    def verify(self, body, sig, **kw):
        kw.setdefault("pubkey_b64", self.pub)
        kw.setdefault("now", NOW)
        return S.verify_and_parse(body, sig, **kw)

    def rejects(self, exc, body, sig, **kw):
        with self.assertRaises(exc) as cm:
            self.verify(body, sig, **kw)
        return cm.exception


class GoodSignature(Base):
    def test_good_signature_produces_record(self):
        body, sig = self.signed()
        b = self.verify(body, sig)
        self.assertEqual(len(b.records), 1)
        r = b.records[0]
        self.assertEqual(r.strategy_key, "silver")
        self.assertEqual(r.coin, "xyz:SILVER")
        self.assertEqual(r.target_weight_bps, 10_000)
        self.assertEqual(r.target_weight_x100, 100)
        # daily bar dated 2026-09-29 closes at 2026-09-30 00:00 UTC
        self.assertEqual(r.bar_close, datetime(2026, 9, 30, tzinfo=UTC))
        self.assertEqual(r.as_of, date(2026, 9, 29))
        self.assertEqual(r.last_action_date, date(2026, 7, 1))
        self.assertEqual(r.script_sha256, SCRIPT)
        self.assertEqual(b.raw, body)
        self.assertEqual(b.signature_b64, sig)
        self.assertEqual(b.body_sha256, hashlib.sha256(body).hexdigest())

    def test_cash_and_2x(self):
        body, sig = self.signed(payload(weight=0, last_action="SELL", last_action_date="1980-01-15"))
        self.assertEqual(self.verify(body, sig).records[0].target_weight_bps, 0)
        body, sig = self.signed(payload(weight=2))
        self.assertEqual(self.verify(body, sig).records[0].target_weight_bps, 20_000)
        body, sig = self.signed(payload(weight=0, last_action="NONE", last_action_date=None))
        self.assertEqual(self.verify(body, sig).records[0].last_action_date, None)

    def test_signature_file_with_trailing_newline_ok(self):
        body, sig = self.signed()
        self.verify(body, (sig + "\n").encode())

    def test_weekend_bar_accepted(self):
        # Monday 00:45 UTC: newest SILVER bar is Friday's (48.75 h old) — within max_bar_age_days
        body, sig = self.signed(payload(as_of="2026-09-25", generated_at="2026-09-28T00:41:00Z"))
        b = self.verify(body, sig, now=datetime(2026, 9, 28, 0, 45, tzinfo=UTC))
        self.assertEqual(b.records[0].bar_close, datetime(2026, 9, 26, tzinfo=UTC))

    def test_unlisted_known_strategy_is_validated_but_not_returned(self):
        extra = {"gold": {"target_weight": 0, "last_action": "NONE", "last_action_date": None, "market": "xyz:GOLD",
                          "status": "holds", "script_sha256": GOLD_SCRIPT}}
        body, sig = self.signed(payload(extra=extra))
        b = self.verify(body, sig)
        self.assertEqual([r.strategy_key for r in b.records], ["silver"])
        self.assertEqual([r.strategy_key for r in b.all_records], ["gold", "silver"])


class Authentication(Base):
    def test_tampered_payload(self):
        body, sig = self.signed()
        tampered = body.replace(b'"target_weight":1', b'"target_weight":2')
        e = self.rejects(S.SignalSignatureInvalid, tampered, sig)
        alerts = e.alert_payloads()
        self.assertEqual(alerts[0]["severity"], "critical")
        self.assertEqual(alerts[0]["coin"], "xyz:SILVER")          # auto-pause new entries on the listed market
        self.assertEqual(alerts[0]["kind"], "signals_signature_invalid")

    def test_wrong_key(self):
        body, sig = self.signed(key=Ed25519PrivateKey.generate())
        self.rejects(S.SignalSignatureInvalid, body, sig)

    def test_bad_signature_encoding(self):
        body, _ = self.signed()
        self.rejects(S.SignalSignatureInvalid, body, "not base64 !!")
        self.rejects(S.SignalSignatureInvalid, body, base64.b64encode(b"x" * 63).decode())
        self.rejects(S.SignalTooLarge, body, "A" * 2000)

    def test_missing_or_bad_pinned_key(self):
        body, sig = self.signed()
        self.rejects(S.SignalConfigError, body, sig, pubkey_b64="")
        self.rejects(S.SignalConfigError, body, sig, pubkey_b64=base64.b64encode(b"k" * 31).decode())
        self.rejects(S.SignalConfigError, body, sig, pubkey_b64="%%%")

    def test_body_over_cap(self):
        body = b" " * (S.MAX_BODY_BYTES + 1)
        self.rejects(S.SignalTooLarge, body, self.signed(body=body)[1])


class Schema(Base):
    def test_weight_3(self):
        self.rejects(S.SignalWeightInvalid, *self.signed(payload(weight=3)))

    def test_other_bad_weights(self):
        self.rejects(S.SignalWeightInvalid, *self.signed(payload(weight=-1)))
        self.rejects(S.SignalWeightInvalid, *self.signed(payload(weight=True)))
        self.rejects(S.SignalWeightInvalid, *self.signed(payload(weight="1")))
        self.rejects(S.SignalMalformed, *self.signed(body=canon(payload()).replace(b'"target_weight":1', b'"target_weight":1.0')))

    def test_unknown_market(self):
        self.rejects(S.SignalMarketMismatch, *self.signed(payload(market="xyz:GOLD")))
        self.rejects(S.SignalMarketMismatch, *self.signed(payload(market="SILVER")))

    def test_unknown_strategy(self):
        extra = {"doge": {"target_weight": 1, "last_action": "BUY", "last_action_date": "2026-01-01", "market": "DOGE",
                          "status": "trades", "script_sha256": "b" * 64}}
        self.rejects(S.SignalUnknownStrategy, *self.signed(payload(extra=extra)))

    def test_non_canonical_bytes(self):
        pretty = json.dumps(payload(), indent=1, sort_keys=True).encode()
        self.rejects(S.SignalMalformed, *self.signed(body=pretty))
        unsorted = json.dumps(payload(), separators=(",", ":")).encode()
        if unsorted != canon(payload()):
            self.rejects(S.SignalMalformed, *self.signed(body=unsorted))

    def test_duplicate_key(self):
        body = canon(payload()).replace(b'{"as_of":"2026-09-29"', b'{"as_of":"2026-09-20","as_of":"2026-09-29"')
        self.rejects(S.SignalMalformed, *self.signed(body=body))

    def test_extra_and_missing_keys(self):
        p = payload(); p["note"] = "x"
        self.rejects(S.SignalMalformed, *self.signed(p))
        p = payload(); p["strategies"]["silver"]["why"] = "breakout"
        self.rejects(S.SignalMalformed, *self.signed(p))
        p = payload(); del p["strategies"]["silver"]["status"]
        self.rejects(S.SignalMalformed, *self.signed(p))
        p = payload(); p["strategies"] = {}
        self.rejects(S.SignalMalformed, *self.signed(p))

    def test_formats(self):
        self.rejects(S.SignalMalformed, *self.signed(payload(as_of="2026-9-29")))
        self.rejects(S.SignalMalformed, *self.signed(payload(as_of="2026-02-30")))
        self.rejects(S.SignalMalformed, *self.signed(payload(generated_at="2026-09-30 00:41:07")))
        self.rejects(S.SignalMalformed, *self.signed(payload(last_action="HOLD")))
        self.rejects(S.SignalMalformed, *self.signed(payload(last_action_date="2026-10-01")))   # after as_of
        self.rejects(S.SignalMalformed, *self.signed(payload(last_action="NONE", weight=0, last_action_date="2026-01-01")))
        self.rejects(S.SignalMalformed, *self.signed(payload(status="paused")))
        self.rejects(S.SignalMalformed, *self.signed(payload(script="E" * 64)))
        self.rejects(S.SignalMalformed, *self.signed(body=canon(payload()).replace(b'"xyz:SILVER"', '"xyz:SILVÉR"'.encode())))

    def test_weight_inconsistent_with_last_action(self):
        self.rejects(S.SignalMalformed, *self.signed(payload(weight=0, last_action="BUY")))
        self.rejects(S.SignalMalformed, *self.signed(payload(weight=1, last_action="SELL")))

    def test_engine_hash(self):
        self.rejects(S.SignalEngineMismatch, *self.signed(payload(engine="0" * 64)))
        body, sig = self.signed()
        self.verify(body, sig, expected_script_sha256={"silver": SCRIPT})
        self.rejects(S.SignalEngineMismatch, body, sig, expected_script_sha256={"silver": "c" * 64})

    def test_listed_strategy_holds_or_missing(self):
        self.rejects(S.SignalStatusNotTrading, *self.signed(payload(status="holds")))
        p = payload(); p["strategies"] = {"gold": {"target_weight": 0, "last_action": "NONE", "last_action_date": None,
                                                     "market": "xyz:GOLD", "status": "holds", "script_sha256": GOLD_SCRIPT}}
        p["engine_sha256"] = engine_of({"gold": GOLD_SCRIPT})
        e = self.rejects(S.SignalMissingStrategy, *self.signed(p))
        self.assertEqual(e.alert_payloads()[0]["coin"], "xyz:SILVER")


class Time(Base):
    def test_stale_generated_at(self):
        body, sig = self.signed(payload(as_of="2026-09-27", generated_at="2026-09-28T08:00:00Z"))
        e = self.rejects(S.SignalStale, body, sig)                                  # 40.75 h > 36 h
        self.assertEqual(e.alert_payloads()[0]["severity"], "critical")

    def test_stale_bar(self):
        body, sig = self.signed(payload(as_of="2026-09-24", generated_at="2026-09-30T00:41:07Z"))
        self.rejects(S.SignalStale, body, sig)                                      # 6 days > 4
        self.verify(body, sig, max_bar_age_days=7)

    def test_future(self):
        self.rejects(S.SignalFromFuture, *self.signed(payload(generated_at="2026-09-30T02:00:00Z")))
        # as_of = today: the bar closes at tomorrow 00:00 UTC — not closed yet
        self.rejects(S.SignalFromFuture, *self.signed(payload(as_of="2026-09-30", last_action_date="2026-09-30")))

    def test_naive_now_refused(self):
        body, sig = self.signed()
        with self.assertRaises(ValueError):
            self.verify(body, sig, now=datetime(2026, 9, 30, 1))


class Continuity(Base):
    def setUp(self):
        super().setUp()
        body, sig = self.signed()
        self.prev = self.verify(body, sig).records[0]

    def test_same_bar_same_weight_ok(self):
        body, sig = self.signed(payload(generated_at="2026-09-30T00:44:00Z"))
        self.verify(body, sig, last_accepted={"silver": self.prev})

    def test_replay_older_bar(self):
        body, sig = self.signed(payload(as_of="2026-09-28", generated_at="2026-09-29T00:41:00Z"))
        self.rejects(S.SignalReplay, body, sig, last_accepted={"silver": self.prev})

    def test_conflict_same_bar_other_weight(self):
        body, sig = self.signed(payload(weight=0, last_action="SELL", last_action_date="2026-09-29"))
        self.rejects(S.SignalConflict, body, sig, last_accepted={"silver": self.prev})

    def test_newer_bar_ok(self):
        prev = replace(self.prev, as_of=date(2026, 9, 28), target_weight_bps=0)
        self.verify(*self.signed(), last_accepted={"silver": prev})


# ---------------------------------------------------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, fail_read: BaseException | None = None):
        self.status_code, self.body, self.headers, self.fail_read, self.closed = status, body, headers or {}, fail_read, False

    def iter_content(self, chunk_size=65536):
        if self.fail_read:
            raise self.fail_read
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        r = self.routes[url]
        if isinstance(r, BaseException):
            raise r
        return r


URL = "https://aijalon-terminal.web.app/signals.json"
SIG_URL = "https://aijalon-terminal.web.app/signals.sig"


class Fetch(Base):
    def fetch(self, routes, **kw):
        kw.setdefault("pubkey_b64", self.pub)
        kw.setdefault("now", NOW)
        kw.setdefault("listed_keys", ("silver",))
        kw.setdefault("max_age_hours", 36)
        sess = FakeSession(routes)
        return S.fetch_signals(URL, session=sess, **kw), sess

    def test_fetch_ok(self):
        body, sig = self.signed()
        b, sess = self.fetch({URL: FakeResp(body=body), SIG_URL: FakeResp(body=sig.encode())})
        self.assertEqual(b.records[0].target_weight_bps, 10_000)
        self.assertEqual([c[0] for c in sess.calls], [URL, SIG_URL])
        for _, kw in sess.calls:
            self.assertIs(kw["allow_redirects"], False)
            self.assertTrue(kw["stream"])
            self.assertEqual(kw["timeout"], S.DEFAULT_TIMEOUT)

    def test_fetch_http_error(self):
        e = None
        with self.assertRaises(S.SignalFetchError) as cm:
            self.fetch({URL: FakeResp(status=404), SIG_URL: FakeResp()})
        e = cm.exception
        self.assertEqual(e.alert_payloads()[0]["severity"], "warn")
        self.assertIsNone(e.alert_payloads()[0]["coin"])
        with self.assertRaises(S.SignalFetchError):                             # redirects are not followed
            self.fetch({URL: FakeResp(status=302, headers={"Location": "https://evil.example/s.json"}), SIG_URL: FakeResp()})

    def test_fetch_timeout(self):
        with self.assertRaises(S.SignalFetchError):
            self.fetch({URL: TimeoutError("read timed out"), SIG_URL: FakeResp()})
        with self.assertRaises(S.SignalFetchError):
            self.fetch({URL: FakeResp(fail_read=ConnectionResetError()), SIG_URL: FakeResp()})
        if S.requests is not None:
            with self.assertRaises(S.SignalFetchError):
                self.fetch({URL: S.requests.Timeout(), SIG_URL: FakeResp()})

    def test_fetch_size_cap(self):
        big = b"x" * (S.MAX_BODY_BYTES + 1)
        with self.assertRaises(S.SignalTooLarge):
            self.fetch({URL: FakeResp(body=b"{}", headers={"Content-Length": str(len(big))}), SIG_URL: FakeResp()})
        resp = FakeResp(body=big)
        with self.assertRaises(S.SignalTooLarge):
            self.fetch({URL: resp, SIG_URL: FakeResp()})
        self.assertTrue(resp.closed)

    def test_https_only(self):
        with self.assertRaises(S.SignalConfigError):
            S.fetch_signals("http://aijalon-terminal.web.app/signals.json", session=FakeSession({}), pubkey_b64=self.pub,
                            now=NOW, listed_keys=("silver",), max_age_hours=36)

    def test_ingest_returns_error_with_alerts(self):
        body, sig = self.signed(payload(weight=3))
        r = S.ingest_signals(URL, session=FakeSession({URL: FakeResp(body=body), SIG_URL: FakeResp(body=sig.encode())}),
                             pubkey_b64=self.pub, now=NOW, listed_keys=("silver",), max_age_hours=36)
        self.assertFalse(r.ok)
        self.assertIsInstance(r.error, S.SignalWeightInvalid)
        self.assertEqual(r.alert_payloads()[0]["kind"], "signals_weight_invalid")
        good_body, good_sig = self.signed()
        r = S.ingest_signals(URL, session=FakeSession({URL: FakeResp(body=good_body), SIG_URL: FakeResp(body=good_sig.encode())}),
                             pubkey_b64=self.pub, now=NOW, listed_keys=("silver",), max_age_hours=36)
        self.assertTrue(r.ok)
        self.assertEqual(r.alert_payloads(), [])

    def test_alert_objects(self):
        try:
            from app.alerts.notifier import Alert, Severity
        except Exception as e:  # noqa: BLE001 - optional module owned elsewhere
            self.skipTest(f"notifier unavailable: {e}")
        body, sig = self.signed()
        with self.assertRaises(S.SignalSignatureInvalid) as cm:
            self.verify(body.replace(b"BUY", b"BUX"), sig)
        a = cm.exception.alerts()
        self.assertIsInstance(a[0], Alert)
        self.assertEqual(a[0].severity, Severity.CRITICAL)
        self.assertEqual(a[0].coin, "xyz:SILVER")

    def test_sig_url_for(self):
        self.assertEqual(S.sig_url_for(URL), SIG_URL)
        self.assertEqual(S.sig_url_for("https://x/feed"), "https://x/feed.sig")


if __name__ == "__main__":
    unittest.main()
