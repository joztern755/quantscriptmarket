from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.security.audit import (
    GENESIS_HASH, AuditEvent, ChainBroken, DbApiAuditSink, InMemoryAuditSink, canonical_json, hash_ip,
    pepper_hash, verify_chain, write_audit,
)

PEPPER = b"p" * 32
T0 = datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)


class CanonicalJsonTests(unittest.TestCase):
    def test_sorted_compact_utf8(self):
        self.assertEqual(canonical_json({"b": 1, "a": [True, None, "é"]}), '{"a":[true,null,"é"],"b":1}'.encode())
        self.assertEqual(canonical_json({"b": {"z": 1, "y": 2}}), canonical_json({"b": {"y": 2, "z": 1}}))

    def test_rejects_floats_and_non_json(self):
        for bad in (1.5, {"a": 0.1}, {"a": [1, float("nan")]}, {1: "x"}, {"a": b"x"}, {"a": {1, 2}}, {"a": datetime.now()}):
            with self.assertRaises(TypeError, msg=repr(bad)):
                canonical_json(bad)

    def test_big_ints_ok(self):
        self.assertEqual(canonical_json({"m": 10**18}), b'{"m":1000000000000000000}')


class IpHashTests(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(hash_ip("1.2.3.4", PEPPER), hash_ip("::ffff:1.2.3.4", PEPPER))
        self.assertEqual(hash_ip("2001:db8::1", PEPPER), hash_ip("2001:0db8:0000:0000:0000:0000:0000:0001", PEPPER))
        self.assertEqual(hash_ip(" 1.2.3.4 ", PEPPER), hash_ip("1.2.3.4", PEPPER))
        self.assertIsNone(hash_ip(None, PEPPER))
        self.assertIsNone(hash_ip("", PEPPER))
        self.assertEqual(len(hash_ip("not-an-ip", PEPPER)), 64)

    def test_peppered(self):
        self.assertNotEqual(hash_ip("1.2.3.4", PEPPER), hash_ip("1.2.3.4", b"q" * 32))
        self.assertNotIn("1.2.3.4", hash_ip("1.2.3.4", PEPPER))
        with self.assertRaises(ValueError):
            hash_ip("1.2.3.4", b"short")

    def test_domain_separation(self):
        self.assertNotEqual(pepper_hash("x", PEPPER, domain="ip"), pepper_hash("x", PEPPER, domain="ua"))


class ChainTests(unittest.TestCase):
    def _write(self, sink, n=3):
        for i in range(n):
            write_audit(sink, "admin:a1", "payout.approve_1", f"payout:{i}", {"amount_micro": 1_000_000 * i, "note": "ok"},
                        "10.0.0.1", pepper=PEPPER, now=T0 + timedelta(seconds=i))

    def test_chain_and_verify(self):
        sink = InMemoryAuditSink()
        self._write(sink)
        self.assertEqual(sink.events[0].prev_hash, GENESIS_HASH)
        self.assertEqual(sink.events[1].prev_hash, sink.events[0].hash)
        self.assertEqual(verify_chain(sink.events), 3)
        self.assertEqual(sink.events[0].created_at, "2026-09-30T07:00:00.000000+00:00")
        self.assertNotIn("10.0.0.1", json.dumps([e.body() for e in sink.events]))

    def test_tamper_detected(self):
        sink = InMemoryAuditSink()
        self._write(sink)
        ev = sink.events[1]
        forged = AuditEvent(ev.actor, ev.action, ev.target, {"amount_micro": 999, "note": "ok"}, ev.ip_hash,
                            ev.created_at, ev.prev_hash, ev.hash)
        with self.assertRaises(ChainBroken):
            verify_chain([sink.events[0], forged, sink.events[2]])
        with self.assertRaises(ChainBroken):
            verify_chain([sink.events[0], sink.events[2]])  # deletion
        with self.assertRaises(ChainBroken):
            verify_chain([sink.events[1], sink.events[0]])  # reorder

    def test_validation(self):
        sink = InMemoryAuditSink()
        with self.assertRaises(TypeError):
            write_audit(sink, "u", "a", "", {"x": 1.0}, None, pepper=PEPPER)
        with self.assertRaises(ValueError):
            write_audit(sink, "", "a", "", {}, None, pepper=PEPPER)
        with self.assertRaises(ValueError):
            write_audit(sink, "u", "a", "", {}, None, pepper=PEPPER, now=datetime(2026, 1, 1))
        self.assertEqual(sink.events, [])  # nothing written on validation failure

    def test_db_api_sink_roundtrip(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, target TEXT,"
                     " payload TEXT, ip_hash TEXT, created_at TEXT, prev_hash TEXT, hash TEXT)")
        sink = DbApiAuditSink(conn, lock_sql=None,
                              insert_sql="INSERT INTO audit_log (actor, action, target, payload, ip_hash, created_at, prev_hash, hash)"
                                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        self._write(sink)
        conn.commit()
        rows = conn.execute("SELECT actor, action, target, payload, ip_hash, created_at, prev_hash, hash FROM audit_log ORDER BY seq").fetchall()
        events = [AuditEvent(a, ac, t, json.loads(p), ip, ca, ph, h) for a, ac, t, p, ip, ca, ph, h in rows]
        self.assertEqual(verify_chain(events), 3)


if __name__ == "__main__":
    unittest.main()
